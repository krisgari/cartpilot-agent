"""
CartPilot marketplace exposed as an MCP server, so any MCP client (Claude Desktop,
another agent) can use the same tools. It reuses the exact tool functions and policy
engine the API agent uses; nothing is reimplemented.

IMPORTANT LIMITATION: over stdio there is no authenticated caller, so `customer_id` is
a plain argument here. The ownership check still applies, but a real deployment must
bind identity from the transport (OAuth / signed token) instead of trusting an argument.

Run:  python mcp_servers/marketplace_server.py     (from the project root)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from cartpilot.agents.tools import build_consumer_tools  # noqa: E402
from cartpilot.marketplace.store import Store  # noqa: E402

mcp = FastMCP("cartpilot-marketplace")
store = Store()


def _tools(customer_id: str) -> dict:
    return build_consumer_tools(store, customer_id)


@mcp.tool()
def get_order(customer_id: str, order_id: str) -> dict:
    """Get a customer's order with item statuses and prices."""
    return _tools(customer_id)["get_my_order"](order_id)


@mcp.tool()
def suggest_substitutions(customer_id: str, order_id: str, item_id: str) -> dict:
    """Ranked substitutes for an out-of-stock item (respects dietary needs)."""
    return _tools(customer_id)["suggest_substitutions"](order_id, item_id)


@mcp.tool()
def request_refund(customer_id: str, order_id: str, item_id: str, reason: str) -> dict:
    """Request a refund. The policy engine returns approved, needs_approval, or rejected."""
    return _tools(customer_id)["request_refund"](order_id, item_id, reason)


@mcp.tool()
def list_escalations() -> list[dict]:
    """List cases waiting for a human."""
    return store.escalations()


if __name__ == "__main__":
    mcp.run()
