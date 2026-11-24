# agent-core

The brain Vector (`../vector-agent`) and Cortana (`../cortana-agent`) share. Each agent is a
separate container with its own persona, tools, safety guard and front end; everything else
is this package, so a fix lands in both.

| Module | What it is |
|---|---|
| `settings.py` | The settings both agents have, and `cfg`, the handle core modules read them through |
| `llm.py` | Chat models for either provider (Claude, or any OpenAI-compatible endpoint) |
| `mcp_tools.py` | Web search (Tavily) and Home Assistant tools over MCP |
| `loop.py` | The ReAct loop (LangGraph Functional API): `build_agent(...)` |
| `skills.py` | Skills: built-in and learned, loaded on demand |
| `activity.py` | The agent's journal and log search tools |
| `compaction.py` | Rolling conversation summary, reading threads back, pruning checkpoints |
| `notes.py` | Today's notes (`memories.json`): remember, forget, the note writer's helpers |
| `beliefs.py` | Client for the belief store on the NUC, the cached snapshot, tools `recall` and `belief_history` |
| `reconcile.py` | The job that files notes and talk as beliefs and rules on conflicts |
| `sleepbatch.py` | Background model calls through the Message Batches API |
| `a2a.py` | The A2A door for other agents, and the ask-the-other-assistant tool |

## How an agent uses it

`app/config.py` loads the shared settings, says who the agent is, and registers itself:

```python
from agent_core import settings
settings.load_shared(globals(), provider_default="anthropic", a2a_port=8096, listen_port=8092)
ASSISTANT_NAME = "Cortana"; LOGGER_NAME = "cortana-agent"; PRONOUNS = ("she", "her", "her"); ...
settings.use(sys.modules[__name__])
```

Every other app module imports `config` first, then the core. `app/llm.py`, `app/skills.py`
and the like are one-line links to the core module of the same name, so `from . import llm`
keeps working everywhere.

What an agent must define is listed at the top of `settings.py`. What it passes to the loop
(tools, always-offered tool names, prompt builders, guard, hooks) is in `loop.build_agent`.

## When things run

The core does not decide when background work happens. Vector runs `reconcile.run(ctx)` and
the conversation folding as sleep jobs; Cortana runs them in her nightly loop.

## Tests

```sh
cd agent-core && python -m pytest tests -q          # the core on its own
cd vector-agent && python -m pytest tests -q
cd cortana-agent && python -m pytest tests -q
```

The images are built with the folder that holds all three as the build context
(`deploy/pi/compose.yaml`), and `deploy/pi/deploy.sh` syncs `agent-core/` to the Pi.
