"""A stand-in for graphskill's MCP server with the two tools qa-review uses."""


from mcp.server.fastmcp import FastMCP

mcp = FastMCP("graphskill")


@mcp.tool()
def symbols_in_file(path: str, compact: bool = False, visibility: str | None = None) -> list:
    if path.endswith("app/pricing.py"):
        return [{"id": "s1", "name": "total", "kind": "function", "location": f"{path}:4-6", "signature": "total(items, discount=0)"}]
    return []


@mcp.tool()
def callers(name: str, depth: int = 1, compact: bool = False) -> list:
    if name == "total":
        return [{"name": "checkout", "location": "app/cart.py:4"}, {"name": "invoice", "location": "app/billing/invoice.py:10"}]
    return []


if __name__ == "__main__":
    mcp.run()
