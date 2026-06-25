import os
import logging
import sqlite3
import asyncio
import threading
import atexit
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("api_server")

# Silence noisy Werkzeug access logs from background polling
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# Load environment variables
load_dotenv(override=True)

# Import the LangGraph agent and MCP lifecycle functions
from agent import agent_runtime, init_mcp_client, close_mcp_client
from langchain_core.messages import HumanMessage

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_company_data.db")

# Setup background event loop and daemon thread
bg_loop = asyncio.new_event_loop()
def start_bg_loop():
    asyncio.set_event_loop(bg_loop)
    bg_loop.run_forever()

t = threading.Thread(target=start_bg_loop, daemon=True)
t.start()

def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, bg_loop)
    return future.result()

# Global services state
checkpointer_ctx = None

async def init_services():
    global checkpointer_ctx
    # Initialize checkpointer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from agent import memory
    checkpointer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints.db")
    checkpointer_ctx = AsyncSqliteSaver.from_conn_string(checkpointer_path)
    real_saver = await checkpointer_ctx.__aenter__()
    memory.target = real_saver
    
    # Initialize MCP client
    await init_mcp_client()

async def close_services():
    global checkpointer_ctx
    # Close MCP client
    await close_mcp_client()
    # Close checkpointer
    if checkpointer_ctx is not None:
        await checkpointer_ctx.__aexit__(None, None, None)
        checkpointer_ctx = None

# Initialize services on background loop
run_async(init_services())

def cleanup():
    logger.info("Application shutting down. Cleaning up services...")
    try:
        run_async(close_services())
    except Exception as e:
        logger.error(f"Error during services cleanup: {e}")
    bg_loop.call_soon_threadsafe(bg_loop.stop)

atexit.register(cleanup)


# Initialize escalations schema
def init_escalations_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            order_id TEXT PRIMARY KEY,
            customer_id TEXT,
            risk_score REAL
        )
    """)
    conn.commit()
    conn.close()

init_escalations_table()

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
    
    # State override check: Block user message if review is pending
    current_state = agent_runtime.get_state(config)
    if current_state and current_state.next and "risk_and_commit" in current_state.next:
        order_id = current_state.values.get("order_id")
        return jsonify({
            "response": "Your transaction return/refund request is currently undergoing review. A supervisor is reviewing your request. Please wait until they approve or reject it.",
            "status": "pending_approval",
            "order_id": order_id
        })

    state_input = {"messages": [HumanMessage(content=message_text)]}

    try:
        # Run agent graph
        result_state = run_async(agent_runtime.ainvoke(state_input, config=config))
        
        # Check if the graph is paused at risk_and_commit node
        current_state = agent_runtime.get_state(config)
        if "risk_and_commit" in current_state.next:
            risk_score = current_state.values.get("risk_score", 0.0)
            order_id = current_state.values.get("order_id")
            
            if risk_score < 0.7:
                # Auto-resume low risk transaction
                logger.info(f"Auto-resuming low risk transaction (score={risk_score}) for {customer_id}")
                result_state = run_async(agent_runtime.ainvoke(None, config=config))
                agent_response = result_state["messages"][-1].content
                return jsonify({"response": agent_response, "status": "completed"})
            else:
                # High risk: Halt graph, update status, and add to escalations
                logger.info(f"Flagged high risk transaction (score={risk_score}) for {customer_id}. Halting for supervisor.")
                agent_runtime.update_state(config, {"resolution_status": "human_escalation"})
                agent_response = "CRITICAL AUDIT: High transaction value flags system risk rules. Halting execution and transferring to a supervisor."
                
                if order_id:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("INSERT OR REPLACE INTO escalations (order_id, customer_id, risk_score) VALUES (?, ?, ?)",
                                   (order_id, customer_id, risk_score))
                    conn.commit()
                    conn.close()
                    
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
        
        cursor.execute("""
            SELECT o.*, e.customer_id, e.risk_score 
            FROM escalations e
            JOIN orders o ON e.order_id = o.order_id
        """)
        rows = cursor.fetchall()
        for row in rows:
            escalations_list.append(dict(row))
        conn.close()
    except Exception as e:
        logger.error(f"Error querying database for escalations: {e}")
        return jsonify({"error": str(e)}), 500
        
    return jsonify(escalations_list)

@app.route("/api/orders", methods=["GET"])
def get_all_orders():
    """
    Returns a list of all orders in the database for the visual database panel.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders")
        rows = cursor.fetchall()
        orders = [dict(row) for row in rows]
        conn.close()
        return jsonify(orders)
    except Exception as e:
        logger.error(f"Error querying all orders: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/approve", methods=["POST"])
def approve_escalation():
    """
    Supervisor approves a pending transaction. Updates state and resumes execution.
    """
    data = request.get_json() or {}
    order_id = data.get("order_id")

    if not order_id:
        return jsonify({"error": "Invalid or missing order_id"}), 400

    customer_id = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT customer_id FROM escalations WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        if row:
            customer_id = row[0]
        conn.close()
    except Exception as e:
        logger.error(f"Error reading escalation from DB: {e}")
        return jsonify({"error": str(e)}), 500

    if not customer_id:
        return jsonify({"error": "Invalid or missing order_id (no pending escalation found)"}), 400

    config = {"configurable": {"thread_id": customer_id}}

    try:
        # Update state to approve (bypass risk validation)
        agent_runtime.update_state(config, {"risk_score": 0.0, "resolution_status": "approved"})
        
        # Resume execution
        result_state = run_async(agent_runtime.ainvoke(None, config=config))
        agent_response = result_state["messages"][-1].content
        
        # Remove from pending escalations
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM escalations WHERE order_id = ?", (order_id,))
        conn.commit()
        conn.close()
        
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

    if not order_id:
        return jsonify({"error": "Invalid or missing order_id"}), 400

    customer_id = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT customer_id FROM escalations WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        if row:
            customer_id = row[0]
        conn.close()
    except Exception as e:
        logger.error(f"Error reading escalation from DB: {e}")
        return jsonify({"error": str(e)}), 500

    if not customer_id:
        return jsonify({"error": "Invalid or missing order_id (no pending escalation found)"}), 400

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
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM escalations WHERE order_id = ?", (order_id,))
        conn.commit()
        conn.close()
        
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

