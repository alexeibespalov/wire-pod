"""A2A (Agent2Agent) front door: other agents can talk to Vector.

* Agent card:   GET  /.well-known/agent-card.json           (public, as A2A expects)
* JSON-RPC:     POST /a2a/jsonrpc   (A2A 1.0 + 0.3 compat)   bearer token required
* REST:              /a2a/rest/...  (A2A 1.0 + 0.3 compat)   bearer token required

Each A2A contextId is its own persistent conversation thread with the same brain,
persona, memory and tools as voice; turns are serialised with voice turns because they
share one robot. Replies go back as text; the model is told it's talking to a remote
agent (app/prompt.py A2A_NOTE). The camera is off for remote agents unless
A2A_ALLOW_CAMERA=1.
"""
import hmac
import logging
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (add_a2a_routes_to_fastapi, create_agent_card_routes,
                               create_jsonrpc_routes, create_rest_routes)
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentProvider,
                       AgentSkill, Part, Task, TaskState, TaskStatus)

from . import config, turn
from .audio import PLAYER

log = logging.getLogger("vector-agent")
_CUE = re.compile(r"\{\{.*?\}\}")


class VectorExecutor(AgentExecutor):
    def __init__(self, get_agent):
        self._get_agent = get_agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        msg, task_id, context_id = context.message, context.task_id, context.context_id
        if not msg or not task_id or not context_id:
            return
        await event_queue.enqueue_event(Task(id=task_id, context_id=context_id,
                                             status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                                             history=[msg]))
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        text = (context.get_user_input() or "").strip()
        if not text:
            await updater.add_artifact(parts=[Part(text="(empty message)")], name="reply", last_chunk=True)
            await updater.complete()
            return
        await updater.start_work(message=updater.new_agent_message(
            parts=[Part(text="Vector is thinking...")]))

        thread = f"a2a-{context_id}"
        async with turn.lock():
            turn.reset(via="a2a")
            log.info("[%s] a2a user: %s", thread, text)
            try:
                result = await self._get_agent().ainvoke(text, {"configurable": {"thread_id": thread}})
                reply = _CUE.sub("", str(result or "")).strip() or "(no reply)"
            except Exception as e:
                log.exception("a2a turn failed")
                reply = f"Sorry, my brain glitched: {type(e).__name__}"
            finally:
                turn.reset()  # back to voice defaults
        log.info("[%s] a2a vector: %s", thread, reply)
        if PLAYER.has_pending():
            PLAYER.kick(0.5)
        await updater.add_artifact(parts=[Part(text=reply)], name="reply", last_chunk=True)
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id or "",
                              context_id=context.context_id or "")
        await updater.cancel()


def agent_card() -> AgentCard:
    url = config.A2A_PUBLIC_URL
    skill = lambda id_, name, desc, ex: AgentSkill(id=id_, name=name, description=desc, tags=["vector", "robot"],
                                                   examples=ex, input_modes=["text"], output_modes=["text"])
    return AgentCard(
        name="Vector",
        description=("Vector, a small home robot with a personality (Anki Vector running WireOS + "
                     "wire-pod). Chat with him, ask questions (he can search the web), and have him "
                     "act at home: show pictures, emoji or drawings on his face, play animations, "
                     "move, or say something out loud. Knows who's in the room via his face recognition."),
        provider=AgentProvider(organization="Vector at home", url=url),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            skill("chat", "Chat with Vector",
                  "Talk to Vector; he answers in character, remembers context per conversation "
                  "and long-term facts, and can search the web.",
                  ["How are you doing today?", "What's the weather tomorrow?"]),
            skill("act", "Do something at home",
                  "Vector acts on his real body at home: show a picture/emoji/drawing on his face, "
                  "play an animation, dance, move, change eye colour.",
                  ["Show a penguin on your face", "Draw a rocket", "Do a happy dance"]),
            skill("announce", "Say something out loud at home",
                  "Vector speaks a message aloud in the room in his own voice.",
                  ["Tell Alexei dinner is ready", "Say good morning to everyone"]),
            skill("presence", "Who's home",
                  "Who Vector can currently see, via his on-device face recognition.",
                  ["Can you see anyone right now?"]),
        ],
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url=f"{url}/a2a/jsonrpc"),
            AgentInterface(protocol_binding="JSONRPC", protocol_version="0.3", url=f"{url}/a2a/jsonrpc"),
            AgentInterface(protocol_binding="HTTP+JSON", protocol_version="1.0", url=f"{url}/a2a/rest"),
            AgentInterface(protocol_binding="HTTP+JSON", protocol_version="0.3", url=f"{url}/a2a/rest"),
        ],
    )


def build_app(get_agent) -> FastAPI:
    card = agent_card()
    handler = DefaultRequestHandler(agent_executor=VectorExecutor(get_agent),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    app = FastAPI()

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path.startswith("/a2a"):
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if not token or not hmac.compare_digest(token, config.A2A_TOKEN):
                log.warning("a2a: rejected request from %s (bad/missing token)",
                            request.client.host if request.client else "?")
                return JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url="/a2a/jsonrpc",
                                             enable_v0_3_compat=True),
        rest_routes=create_rest_routes(request_handler=handler, path_prefix="/a2a/rest",
                                       enable_v0_3_compat=True),
    )
    return app
