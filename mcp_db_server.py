import os
import sqlite3
import json
import sys
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("Orders DB Server")

# Resolve DB path relative to script directory
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_company_data.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Clean ASCII database validation
def verify_existing_database():
    if not os.path.exists(DB_PATH):
        print(f"CRITICAL ERROR: DB file '{DB_PATH}' not found.", file=sys.stderr)
        sys.exit(1)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders';")
        if not cursor.fetchone():
            print("WARNING: Table 'orders' not found.", file=sys.stderr)
        conn.close()
    except Exception as e:
        print(f"Database connection failed: {e}", file=sys.stderr)
        sys.exit(1)

# Run verification immediately
verify_existing_database()

@mcp.tool()
def get_order_details(order_id: str) -> str:
    """
    Retrieve all details for a specific order by its order ID.
    
    Args:
        order_id: The unique ID of the order (e.g., 'ABC-123').
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        if not row:
            return f"Error: Order {order_id} not found."
        
        details = dict(row)
        return json.dumps(details, indent=2)
    except Exception as e:
        return f"Error querying database: {str(e)}"
    finally:
        conn.close()

@mcp.tool()
def list_all_orders() -> str:
    """
    Retrieve a summary list of all orders in the database.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT order_id, item_name, item_value, fulfillment_status, days_since_delivery FROM orders")
        rows = cursor.fetchall()
        orders = [dict(row) for row in rows]
        return json.dumps(orders, indent=2)
    except Exception as e:
        return f"Error querying database: {str(e)}"
    finally:
        conn.close()

@mcp.tool()
def cancel_order(order_id: str) -> str:
    """
    Cancel an order. Cancellation is only allowed if the order is 'unshipped'.
    
    Args:
        order_id: The unique ID of the order to cancel.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT fulfillment_status FROM orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        if not row:
            return f"Error: Order {order_id} not found."
        
        status = row['fulfillment_status']
        if status == 'cancelled':
            return f"Order {order_id} is already cancelled."
        elif status == 'returned':
            return f"Error: Order {order_id} was already returned and cannot be cancelled."
        elif status != 'unshipped':
            return f"Error: Order {order_id} cannot be cancelled because its status is '{status}'."
        
        cursor.execute("UPDATE orders SET fulfillment_status = 'cancelled' WHERE order_id = ?", (order_id,))
        conn.commit()
        return f"Success: Order {order_id} has been successfully cancelled and refunded."
    except Exception as e:
        return f"Error: {str(e)}"
    finally:
        conn.close()

@mcp.tool()
def return_order(order_id: str) -> str:
    """
    Return a delivered order. Return is only allowed if:
    1. The order's fulfillment_status is 'delivered'.
    2. The item was delivered within the last 30 days (days_since_delivery <= 30).
    
    Args:
        order_id: The unique ID of the order to return.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT fulfillment_status, days_since_delivery FROM orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        if not row:
            return f"Error: Order {order_id} not found."
        
        status = row['fulfillment_status']
        days = row['days_since_delivery']
        
        if status == 'returned':
            return f"Order {order_id} is already returned."
        elif status == 'cancelled':
            return f"Error: Order {order_id} was cancelled and cannot be returned."
        elif status != 'delivered':
            return f"Error: Order {order_id} cannot be returned because it has not been delivered yet (current status: '{status}')."
        
        if days > 30:
            return f"Error: Order {order_id} cannot be returned because it was delivered {days} days ago, which exceeds the 30-day return policy window."
        
        cursor.execute("UPDATE orders SET fulfillment_status = 'returned' WHERE order_id = ?", (order_id,))
        conn.commit()
        return f"Success: Return processed for order {order_id}. Refund initiated."
    except Exception as e:
        return f"Error: {str(e)}"
    finally:
        conn.close()

@mcp.tool()
def simulate_days_since_delivery(order_id: str, days: int) -> str:
    """
    Update the 'days_since_delivery' field for testing and policy simulation.
    
    Args:
        order_id: The unique ID of the order to update.
        days: Number of days since delivery (integer).
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,))
        if not cursor.fetchone():
            return f"Error: Order {order_id} not found."
        
        cursor.execute("UPDATE orders SET days_since_delivery = ? WHERE order_id = ?", (days, order_id))
        conn.commit()
        return f"Success: Days since delivery for order {order_id} updated to {days}."
    except Exception as e:
        return f"Error: {str(e)}"
    finally:
        conn.close()

if __name__ == "__main__":
    mcp.run()
