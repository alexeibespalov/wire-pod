"""Remote MCP tools: Tavily web search, and Home Assistant (its built-in MCP Server).

Keys/tokens are sent as Authorization headers, never in URLs. Loaded once at startup;
a server that's unreachable is simply left out (the agent runs without its tools).

Home Assistant tools get a `home_` prefix (e.g. home_HassTurnOn; the adapter's
tool_name_prefix with server name "home", the MCP call keeps the real name): the
'home' skill unlocks them all (skills/home/SKILL.md `tools: home_*`), so they cost
nothing on turns that aren't about the house. Home Assistant only offers what you've
exposed to Assist (Settings > Voice assistants > Expose): that's the main safety switch.
"""
import logging

from langchain_mcp_adapters.client import MultiServerMCPClient

from . import config

log = logging.getLogger("vector-agent")

HOME_PREFIX = "home_"


async def _load(name: str, conn: dict, prefix: bool = False) -> list:
    try:
        return await MultiServerMCPClient({name: conn}, tool_name_prefix=prefix).get_tools()
    except Exception as e:
        log.warning("could not load %s MCP tools (%s): left out", name, e)
        return []


async def load_mcp_tools() -> list:
    out = []
    if config.TAVILY_API_KEY:
        tools = await _load("tavily", {
            "transport": "streamable_http",
            "url": config.TAVILY_MCP_URL,
            "headers": {
                "Authorization": f"Bearer {config.TAVILY_API_KEY}",
                "DEFAULT_PARAMETERS": config.TAVILY_DEFAULTS,
            },
            "timeout": 20,
        })
        wanted = [t for t in tools if t.name in config.TAVILY_TOOLS]
        log.info("MCP tools loaded: %s (available: %s)", [t.name for t in wanted], [t.name for t in tools])
        out += wanted
    else:
        log.info("TAVILY_API_KEY not set: web search disabled")
    if config.HA_TOKEN:
        tools = await _load("home", {
            "transport": "streamable_http",
            "url": config.HA_MCP_URL,
            "headers": {"Authorization": f"Bearer {config.HA_TOKEN}"},
            "timeout": 15,
        }, prefix=True)                         # server "home" -> home_HassTurnOn, ...
        log.info("Home Assistant tools loaded: %s", [t.name for t in tools])
        out += tools
    else:
        log.info("HA_TOKEN not set: Home Assistant disabled")
    return out
