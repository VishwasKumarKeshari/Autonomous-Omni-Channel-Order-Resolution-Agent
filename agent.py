import os
import json
from typing import Literal, TypedDict, Dict, Any
from typing_extensions import Annotated
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

# Import the official client adapter to bridge LangGraph with external MCP servers

from langchain_mcp_adapters.client import MultiServerMCPClient

# ==========================================================
# 1. STATE CONFIGURATION & ENVIRONMENT MANAGEMENT
# ==========================================================
DB_FILE = "my_company_data.db"

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    order_id: str
    intent: str
    risk_score: float
    resolution_status: str

# Pydantic schema for structured intent extraction
class RouteInput(BaseModel):
    intent: Literal["cancel_order", "return_refund", "order_status", "unknown"] = Field(
        description="The target category of the user's transaction request."
    )
    order_id: str = Field(
        default="", description="The alphanumeric order ID if mentioned in the message text."
    )

# Setup LLM processor using Google Gemini
llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)

# Configure stdio connection parameters to launch the MCP database server
MCP_CONFIG = {
    "local-db-mcp-service": {
        "transport": "stdio",
        "command": "python",
        "args": [os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_db_server.py")]
    }
}

# ==========================================================
# 2. GRAPH NODE WORKFLOWS (Leveraging MCP Tools)
# ==========================================================
def router_node(state: AgentState) -> Dict[str, Any]:
    """Analyzes user intent and extracts order metadata parameters."""
    structured_llm = llm.with_structured_output(RouteInput)
    last_message = state["messages"][-1].content
    extraction = structured_llm.invoke([HumanMessage(content=f"Analyze support request text: '{last_message}'")])
    order_id = extraction.order_id if extraction.order_id else state.get("order_id", "")
    
    intent = extraction.intent
    if intent == "unknown" and state.get("intent") and state.get("intent") != "unknown":
        intent = state.get("intent")
        
    return {
        "intent": intent,
        "order_id": order_id,
        "resolution_status": "",
        "risk_score": 0.0
    }

async def process_cancel_node(state: AgentState) -> Dict[str, Any]:
    """Applies store cancel policies by fetching data live over the MCP server link."""
    order_id = state.get("order_id")
    if not order_id:
        return {
            "messages": [AIMessage(content="Please provide your order ID so I can process your cancellation.")],
            "resolution_status": "completed"
        }
        
    mcp_client = MultiServerMCPClient(MCP_CONFIG)
    async with mcp_client.session("local-db-mcp-service") as session:
        db_res = await session.call_tool("get_order_details", {"order_id": order_id})
        
    db_response = db_res.content[0].text
    if db_response.startswith("Error"):
        return {"messages": [AIMessage(content=db_response)], "intent": "unknown"}
        
    data = json.loads(db_response) 
    
    # Policy check: Shipped/delivered items can't be cancelled directly
    if data["fulfillment_status"] in ["shipped", "delivered"]:
        return {
            "intent": "return_refund",
            "messages": [AIMessage(content=f"Order {order_id} has already shipped out. Rerouting this to our return pipeline.")]
        }
        
    return {
        "messages": [AIMessage(content=f"Order {order_id} verified ({data['item_name']}). Checking validation policies.")],
        "risk_score": 0.1  # Low risk for standard unshipped cancel requests
    }

async def process_return_node(state: AgentState) -> Dict[str, Any]:
    """Handles returns and evaluates transaction risk over pre-stored items."""
    order_id = state.get("order_id")
    if not order_id:
        return {
            "messages": [AIMessage(content="Please provide your order ID to begin your return.")],
            "resolution_status": "completed"
        }
        
    mcp_client = MultiServerMCPClient(MCP_CONFIG)
    async with mcp_client.session("local-db-mcp-service") as session:
        db_res = await session.call_tool("get_order_details", {"order_id": order_id})
        
    db_response = db_res.content[0].text
    if db_response.startswith("Error"):
        return {"messages": [AIMessage(content=db_response)], "intent": "unknown"}
        
    data = json.loads(db_response)
    
    # Store compliance rule check: 30-day time cap window
    if data["days_since_delivery"] > 30:
        return {
            "resolution_status": "rejected",
            "messages": [AIMessage(content=f"Order {order_id} was delivered {data['days_since_delivery']} days ago. This is outside our 30-day company policy.")]
        }
        
    # Evaluate high-ticket risk rules
    risk = 0.8 if float(data["item_value"]) > 500 else 0.2
    return {
        "messages": [AIMessage(content=f"Return workflow initialized for {data['item_name']}. Evaluating fraud parameters.")],
        "risk_score": risk
    }

async def general_status_node(state: AgentState) -> Dict[str, Any]:
    """Provides safe read-only file status lookups over the system network."""
    order_id = state.get("order_id")
    if not order_id:
        return {
            "messages": [AIMessage(content="Please provide your order ID to check status tracking records.")],
            "resolution_status": "completed"
        }
        
    mcp_client = MultiServerMCPClient(MCP_CONFIG)
    async with mcp_client.session("local-db-mcp-service") as session:
        db_res = await session.call_tool("get_order_details", {"order_id": order_id})
        
    db_response = db_res.content[0].text
    if db_response.startswith("Error"):
        return {"messages": [AIMessage(content=db_response)], "intent": "unknown"}
        
    data = json.loads(db_response)
    return {
        "messages": [AIMessage(content=f"Status for order {order_id}: {data['fulfillment_status'].upper()}. Delivered {data['days_since_delivery']} days ago.")],
        "resolution_status": "completed"
    }

async def risk_and_commit_node(state: AgentState) -> Dict[str, Any]:
    """Executes state changes or handles manager rejections."""
    intent = state.get("intent")
    order_id = state.get("order_id")
    resolution_status = state.get("resolution_status")
    
    if resolution_status == "rejected":
        return {
            "resolution_status": "completed",
            "messages": [AIMessage(content=f"Your request for order {order_id} has been declined by a manager.")]
        }
        
    # Process transactional changes by calling specific write tools over the network channel
    mcp_client = MultiServerMCPClient(MCP_CONFIG)
    async with mcp_client.session("local-db-mcp-service") as session:
        if intent == "cancel_order":
            db_res = await session.call_tool("cancel_order", {"order_id": order_id})
        else:  # return_refund
            db_res = await session.call_tool("return_order", {"order_id": order_id})
            
    db_mutation_result = db_res.content[0].text
    
    return {
        "resolution_status": "completed",
        "messages": [AIMessage(content=f"Success! {db_mutation_result}")]
    }

def fallback_node(state: AgentState) -> Dict[str, Any]:
    """Handles messages where intent is unknown or unstructured."""
    return {
        "messages": [AIMessage(content="I'm sorry, I didn't quite catch that. I can help you check your order status, request a cancellation, or process a return. Please provide your order ID to get started.")],
        "resolution_status": "completed"
    }

# ==========================================================
# 3. GRAPH CONDITIONAL ROUTING MAP AND COMPILATION
# ==========================================================
def route_intent_condition(state: AgentState) -> Literal["cancel_order", "return_refund", "order_status", "unknown"]:
    return state.get("intent", "unknown")

def route_risk_condition(state: AgentState) -> Literal["commit", "end"]:
    if state.get("resolution_status") in ["completed", "rejected"]:
        return "end"
    return "commit"

# Build Core Graph Framework
builder = StateGraph(AgentState)
builder.add_node("router", router_node)
builder.add_node("cancel_order", process_cancel_node)
builder.add_node("return_refund", process_return_node)
builder.add_node("order_status", general_status_node)
builder.add_node("fallback", fallback_node)
builder.add_node("risk_and_commit", risk_and_commit_node)

builder.add_edge(START, "router")
builder.add_conditional_edges("router", route_intent_condition, {
    "cancel_order": "cancel_order", "return_refund": "return_refund", "order_status": "order_status", "unknown": "fallback"
})
builder.add_conditional_edges("cancel_order", route_risk_condition, {"commit": "risk_and_commit", "end": END})
builder.add_conditional_edges("return_refund", route_risk_condition, {"commit": "risk_and_commit", "end": END})
builder.add_edge("order_status", END)
builder.add_edge("fallback", END)
builder.add_edge("risk_and_commit", END)

# Compile using short term memory checkpointer to store chat context tracks
# Interrupt BEFORE running the risk_and_commit node to allow supervisor review of risk score
memory = MemorySaver()
agent_runtime = builder.compile(checkpointer=memory, interrupt_before=["risk_and_commit"])
