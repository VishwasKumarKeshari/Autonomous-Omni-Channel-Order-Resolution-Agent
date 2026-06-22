import os
import logging
import sqlite3
import asyncio
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("api_server")

# Load environment variables
load_dotenv(override=True)

# Import the LangGraph agent
from agent import agent_runtime
from langchain_core.messages import HumanMessage

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_company_data.db")

# Registry mapping order_id -> {"customer_id": str, "risk_score": float}
pending_escalations = {}

@app.route("/", methods=["GET"])
def index():
    """
    Serve the main control dashboard.
    """
    return render_template("index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Endpoint for customer chat. Receives a message and routes it through the agent.
    """
    data = request.get_json() or {}
    customer_id = data.get("customer_id")
    message_text = data.get("message", "").strip()

    if not customer_id or not message_text:
        return jsonify({"error": "Missing customer_id or message"}), 400

    logger.info(f"Chat request from {customer_id}: {message_text}")

    config = {"configurable": {"thread_id": customer_id}}
    state_input = {"messages": [HumanMessage(content=message_text)]}

    try:
        # Run agent graph
        result_state = asyncio.run(agent_runtime.ainvoke(state_input, config=config))
        
        # Check if the graph is paused at risk_and_commit node
        current_state = agent_runtime.get_state(config)
        if "risk_and_commit" in current_state.next:
            risk_score = current_state.values.get("risk_score", 0.0)
            order_id = current_state.values.get("order_id")
            
            if risk_score < 0.7:
                # Auto-resume low risk transaction
                logger.info(f"Auto-resuming low risk transaction (score={risk_score}) for {customer_id}")
                result_state = asyncio.run(agent_runtime.ainvoke(None, config=config))
                agent_response = result_state["messages"][-1].content
                return jsonify({"response": agent_response, "status": "completed"})
            else:
                # High risk: Halt graph, update status, and add to escalations
                logger.info(f"Flagged high risk transaction (score={risk_score}) for {customer_id}. Halting for supervisor.")
                agent_runtime.update_state(config, {"resolution_status": "human_escalation"})
                agent_response = "CRITICAL AUDIT: High transaction value flags system risk rules. Halting execution and transferring to a supervisor."
                
                if order_id:
                    pending_escalations[order_id] = {
                        "customer_id": customer_id,
                        "risk_score": risk_score
                    }
                return jsonify({
                    "response": agent_response,
                    "status": "pending_approval",
                    "order_id": order_id
                })
        else:
            agent_response = result_state["messages"][-1].content
            return jsonify({"response": agent_response, "status": "completed"})

    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/escalations", methods=["GET"])
def get_escalations():
    """
    Returns a list of all currently pending supervisor escalations with details from the database.
    """
    escalations_list = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        for order_id, info in pending_escalations.items():
            cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = cursor.fetchone()
            if row:
                order_details = dict(row)
                order_details["customer_id"] = info["customer_id"]
                order_details["risk_score"] = info["risk_score"]
                escalations_list.append(order_details)
        conn.close()
    except Exception as e:
        logger.error(f"Error querying database for escalations: {e}")
        return jsonify({"error": str(e)}), 500
        
    return jsonify(escalations_list)

@app.route("/api/approve", methods=["POST"])
def approve_escalation():
    """
    Supervisor approves a pending transaction. Updates state and resumes execution.
    """
    data = request.get_json() or {}
    order_id = data.get("order_id")

    if not order_id or order_id not in pending_escalations:
        return jsonify({"error": "Invalid or missing order_id"}), 400

    info = pending_escalations[order_id]
    customer_id = info["customer_id"]
    config = {"configurable": {"thread_id": customer_id}}

    try:
        # Update state to approve (bypass risk validation)
        agent_runtime.update_state(config, {"risk_score": 0.0, "resolution_status": "approved"})
        
        # Resume execution
        result_state = asyncio.run(agent_runtime.ainvoke(None, config=config))
        agent_response = result_state["messages"][-1].content
        
        # Remove from pending escalations
        pending_escalations.pop(order_id, None)
        logger.info(f"Supervisor approved return for order {order_id}.")
        return jsonify({"status": "success", "response": agent_response})
    except Exception as e:
        logger.error(f"Error approving escalation for order {order_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/decline", methods=["POST"])
def decline_escalation():
    """
    Supervisor declines a pending transaction. Updates state to rejected and resumes execution.
    """
    data = request.get_json() or {}
    order_id = data.get("order_id")

    if not order_id or order_id not in pending_escalations:
        return jsonify({"error": "Invalid or missing order_id"}), 400

    info = pending_escalations[order_id]
    customer_id = info["customer_id"]
    config = {"configurable": {"thread_id": customer_id}}

    try:
        from langchain_core.messages import AIMessage
        
        # Update state directly to append decline message and mark as completed
        agent_runtime.update_state(
            config,
            {
                "resolution_status": "completed",
                "messages": [AIMessage(content=f"Your request for order {order_id} has been declined by a manager.")]
            },
            as_node="risk_and_commit"
        )
        
        # Retrieve the updated state response
        current_state = agent_runtime.get_state(config)
        agent_response = current_state.values["messages"][-1].content
        
        # Remove from pending escalations
        pending_escalations.pop(order_id, None)
        logger.info(f"Supervisor declined return for order {order_id}.")
        return jsonify({"status": "success", "response": agent_response})
    except Exception as e:
        logger.error(f"Error declining escalation for order {order_id}: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # If running on Hugging Face (detected by SPACE_ID), default to 7860, otherwise 5000 locally
    default_port = 7860 if "SPACE_ID" in os.environ else 5000
    port = int(os.environ.get("PORT", default_port))
    app.run(host="0.0.0.0", port=port, debug=True)

