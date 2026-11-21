"""Remote MCP tools (currently: Tavily web search).

The API key is sent as an Authorization header, never in the URL.
Loaded once at startup; if the server is unreachable the agent runs without them.
"""
import logging

from langchain_mcp_adapters.client import MultiServerMCPClient

from . import config

log = logging.getLogger("vector-agent")


async def load_mcp_tools() -> list:
    if not config.TAVILY_API_KEY:
        log.info("TAVILY_API_KEY not set: web search disabled")
        return []
    client = MultiServerMCPClient({
        "tavily": {
            "transport": "streamable_http",
            "url": config.TAVILY_MCP_URL,
            "headers": {
                "Authorization": f"Bearer {config.TAVILY_API_KEY}",
                "DEFAULT_PARAMETERS": config.TAVILY_DEFAULTS,
            },
            "timeout": 20,
        }
    })
    try:
        tools = await client.get_tools()
    except Exception as e:
        log.warning("could not load Tavily MCP tools (%s): web search disabled", e)
        return []
    wanted = [t for t in tools if t.name in config.TAVILY_TOOLS]
    log.info("MCP tools loaded: %s (available: %s)",
             [t.name for t in wanted], [t.name for t in tools])
    return wanted
