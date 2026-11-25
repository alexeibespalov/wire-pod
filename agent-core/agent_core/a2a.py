"""A2A (Agent2Agent): this agent's front door for other agents, and how it asks another one.

Server (build_app):
* Agent card:   GET  /.well-known/agent-card.json           (public, as A2A expects)
* JSON-RPC:     POST /a2a/jsonrpc   (A2A 1.0 + 0.3 compat)   bearer token required
* REST:              /a2a/rest/...  (A2A 1.0 + 0.3 compat)   bearer token required
Each A2A contextId is its own persistent conversation thread with the same brain, memory
and tools as voice. The agent supplies `run_turn(text, thread) -> reply`, which holds its
own turn lock and sets its channel to "a2a". Replies go back as text.

Client (peer_tool): one tool, named after the other agent ("ask_cortana"), that sends it a
question and returns the reply. It is never offered during a turn that itself arrived over
A2A (see `hidden_on_a2a`), so two agents cannot bounce a question back and forth.
"""
import hmac
import logging
import re
import uuid
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langchain_core.tools import StructuredTool

from .settings import cfg as config
from . import dash

log = logging.getLogger(config.LOGGER_NAME)
_CUE = re.compile(r"\{\{.*?\}\}")


# ------------------------------------------------------------------------ server
def interfaces(url: str) -> list:
    from a2a.types import AgentInterface
    return [AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url=f"{url}/a2a/jsonrpc"),
            AgentInterface(protocol_binding="JSONRPC", protocol_version="0.3", url=f"{url}/a2a/jsonrpc"),
            AgentInterface(protocol_binding="HTTP+JSON", protocol_version="1.0", url=f"{url}/a2a/rest"),
            AgentInterface(protocol_binding="HTTP+JSON", protocol_version="0.3", url=f"{url}/a2a/rest")]


def build_app(card, run_turn: Callable[[str, str], Awaitable[str]], *, token: str | None = None,
              thinking: str | None = None) -> FastAPI:
    """The A2A app for this agent. `run_turn(text, thread)` runs one turn and returns the reply."""
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (add_a2a_routes_to_fastapi, create_agent_card_routes,
                                   create_jsonrpc_routes, create_rest_routes)
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.tasks.task_updater import TaskUpdater
    from a2a.types import Part, Task, TaskState, TaskStatus

    secret = token if token is not None else config.A2A_TOKEN
    name = config.ASSISTANT_NAME

    class Executor(AgentExecutor):
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
                parts=[Part(text=thinking or f"{name} is thinking...")]))
            thread = f"a2a-{context_id}"
            log.info("[%s] a2a user: %s", thread, text)
            try:
                with dash.a2a_exchange():
                    reply = _CUE.sub("", str(await run_turn(text, thread) or "")).strip() or "(no reply)"
            except Exception as e:
                log.exception("a2a turn failed")
                reply = f"Sorry, my brain glitched: {type(e).__name__}"
            log.info("[%s] a2a %s: %s", thread, name.lower(), reply)
            await updater.add_artifact(parts=[Part(text=reply)], name="reply", last_chunk=True)
            await updater.complete()

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id or "",
                                  context_id=context.context_id or "")
            await updater.cancel()

    handler = DefaultRequestHandler(agent_executor=Executor(), task_store=InMemoryTaskStore(), agent_card=card)
    app = FastAPI()

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path.startswith("/a2a"):
            auth = request.headers.get("authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if not given or not secret or not hmac.compare_digest(given, secret):
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


# ------------------------------------------------------------------------ client
def _texts(obj) -> list[str]:
    """Every text part in an A2A result, in order (a Task's artifacts, or a Message's parts)."""
    out: list[str] = []
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str):
            out.append(obj["text"])
        for k, v in obj.items():
            if k not in ("history", "status"):      # history = our own question; status = "thinking..."
                out += _texts(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _texts(v)
    return out


async def ask(url: str, token: str, text: str, *, timeout: float, context_id: str | None = None,
              transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Send one message to another agent's A2A door and return its reply text."""
    payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
        **({"contextId": context_id} if context_id else {}),
        "parts": [{"kind": "text", "text": text}]}}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3), transport=transport) as client:
        r = await client.post(f"{url}/a2a/jsonrpc", json=payload,
                              headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise PermissionError("the other agent refused the token")
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"].get("message") or data["error"])[:200])
    texts = [t for t in _texts(data.get("result")) if t.strip()]
    return texts[-1].strip() if texts else ""


def peer_tool(channel: Callable[[], str]):
    """The ask-the-other-agent tool, or None when no peer is configured (PEER_NAME,
    PEER_A2A_URL, PEER_A2A_TOKEN)."""
    peer, url, token = config.PEER_NAME, config.PEER_A2A_URL, config.PEER_A2A_TOKEN
    if not (peer and url and token):
        return None
    me = config.ASSISTANT_NAME
    # one conversation per day with the peer, so it keeps the thread of what was asked
    ctx = lambda: f"{me.lower()}-{__import__('datetime').date.today().isoformat()}"   # noqa: E731

    async def _ask(question: str) -> str:
        question = " ".join(question.split())[:600]
        if not question:
            return "error: ask something"
        if channel() == "a2a":
            return f"error: you can't ask {peer} while you are answering another agent"
        try:
            with dash.a2a_exchange():
                reply = await ask(url, token, f"This is {me}, the family's other assistant. {question}",
                                  timeout=config.PEER_TIMEOUT_S, context_id=ctx())
        except (httpx.TimeoutException, TimeoutError):
            return f"{peer} didn't answer in time (probably busy talking). Carry on without it and say so."
        except Exception as e:
            log.warning("a2a: asking %s failed: %s", peer, e)
            return f"error: couldn't reach {peer} right now ({type(e).__name__}). Carry on without it and say so."
        return f"{peer} says: {reply}" if reply else f"{peer} had nothing to say about that."

    return StructuredTool.from_function(
        coroutine=_ask, name=f"ask_{peer.lower()}",
        description=(
            f"Ask {peer}, the family's other assistant at home, a question and get the answer as text. "
            f"{peer} has a separate memory: use this when someone asks what {peer} knows or remembers, "
            f"or when your own recall found nothing about a person or the home and {peer} might know. "
            f"Ask one clear question (\"What do you remember about Leo's favourite game?\"). Say a few "
            f"words first (\"Let me ask {peer}.\") because it takes a few seconds. At most once per "
            f"reply. What comes back is what {peer} believes, not a fact you checked yourself: if you "
            f"keep it, remember it as \"{peer} says ...\"."))


def hidden_on_a2a(ch: str) -> set[str]:
    """Tool names not offered on a turn that arrived over A2A: never ask back."""
    return {f"ask_{config.PEER_NAME.lower()}"} if ch == "a2a" and config.PEER_NAME else set()
