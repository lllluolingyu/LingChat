"""LingChat WebSocket bridge — drive a LingCore agent from a browser.

LingChat lives *outside* the ``lingcore`` package on purpose: the core stays
frontend-agnostic (CLAUDE.md invariant 3). This module is a thin adapter that
implements the same contract the CLI does — stream ``AgentEvent``s out, and
answer the agent's ``confirm`` callback — but over a WebSocket instead of a
terminal.

The one subtlety is the confirmation round-trip. While ``agent.run`` is
streaming, a tool (e.g. ``run_shell``) may call ``ctx.confirm`` and block until
the human answers. The browser's answer arrives as a separate inbound message,
so a single **reader task** owns the socket: it starts agent turns and resolves
pending confirm futures, letting a confirm reply be read *concurrently* with an
in-flight run.

Sessions: the app opens the profile's ``SessionStore`` once (history lives in
``<profile_dir>/sessions.db``; see ``lingcore.sessions``). Each WebSocket may
carry ``?session=<id>`` to resume; without it a fresh id is allocated. Ids are
authoritative in the ``hello`` message — an unknown-but-well-formed client id
is simply adopted (rows are lazy, so reconnecting before ever speaking costs
nothing). A session already attached in this process is refused with
``session_busy`` so two tabs cannot interleave one transcript. REST endpoints
under ``/api/sessions`` serve the sidebar: list, transcript, rename, delete.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from lingcore.agent import Agent
from lingcore.config import AgentProfile
from lingcore.errors import SessionError
from lingcore.events import (
    AgentEvent,
    Error,
    Final,
    SkillActivated,
    StreamRetry,
    TextDelta,
    ToolCallStarted,
    ToolResultEvent,
)
from lingcore.message import Message
from lingcore.sessions import SessionStore, is_session_id, new_session_id, open_store

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# An optional zero-arg factory returning an LLMClient-shaped object. When given,
# each session uses it instead of building a real client — the seam tests use to
# drive the bridge with a scripted fake, and embedders can use to inject a
# custom backend.
LLMFactory = Callable[[], Any]


def _event_to_msg(event: AgentEvent) -> dict[str, Any]:
    """Map an AgentEvent to a JSON-serializable message for the browser."""
    match event:
        case TextDelta(text):
            return {"type": "text", "text": text}
        case ToolCallStarted(call):
            return {"type": "tool_call", "name": call.name, "arguments": call.arguments}
        case ToolResultEvent(result):
            return {
                "type": "tool_result",
                "name": result.name,
                "ok": result.ok,
                "content": result.content,
            }
        case SkillActivated(name, active):
            return {"type": "skill", "name": name, "active": active}
        case StreamRetry(attempt, max_attempts, reason, discarded_chars):
            return {
                "type": "stream_retry",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "reason": reason,
                "discarded_chars": discarded_chars,
            }
        case Final(content):
            return {"type": "final", "text": content}
        case Error(message):
            return {"type": "error", "message": message}
    return {"type": "unknown"}  # pragma: no cover - exhaustive match above


def _stored_to_display(m: Message) -> dict[str, Any]:
    """Map one stored message to the shape the transcript endpoint serves.

    ``ToolResult.ok`` is not stored on ``Message``; the loop encodes failures
    as an ``"ERROR: "`` content prefix (its own convention), which is what
    ``ok`` reflects here.
    """
    if m.role == "user":
        return {"role": "user", "text": m.content}
    if m.role == "assistant":
        return {
            "role": "assistant",
            "text": m.content,
            "tool_calls": [
                {"name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
            ],
        }
    return {
        "role": "tool",
        "name": m.name,
        "ok": not m.content.startswith("ERROR: "),
        "content": m.content,
    }


class _RenameBody(BaseModel):
    title: str


class WebSession:
    """One browser connection: owns an Agent and bridges it to the socket."""

    def __init__(
        self,
        ws: WebSocket,
        profile: AgentProfile,
        base_dir: Path,
        llm_factory: LLMFactory | None = None,
        store: SessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.ws = ws
        # Per-connection tool_options dict so a future "allow always" stays
        # isolated to this session (mirrors the CLI composition root).
        self._tool_options = dict(profile.tool_options)
        self._store = store
        self._session_id = session_id
        self.agent = Agent.from_profile(
            profile,
            confirm=self.confirm,
            base_dir=base_dir,
            tool_options=self._tool_options,
            llm=llm_factory() if llm_factory is not None else None,
            session_store=store,
            session_id=session_id,
        )
        self.profile = profile
        self._pending_confirm: asyncio.Future[bool] | None = None
        self._run_lock = asyncio.Lock()

    async def confirm(self, command: str) -> bool:
        """Ask the browser to approve a command; await its click."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending_confirm = fut
        await self.ws.send_json({"type": "confirm", "command": command})
        try:
            return await fut
        finally:
            self._pending_confirm = None

    async def _run_turn(self, text: str) -> None:
        # Serialize turns so one connection's runs share memory safely.
        async with self._run_lock:
            try:
                async for event in self.agent.run(text):
                    await self.ws.send_json(_event_to_msg(event))
            except WebSocketDisconnect:
                raise
            except Exception as e:  # never let one turn kill the connection
                await self.ws.send_json({"type": "error", "message": f"internal error: {e!r}"})
            await self.ws.send_json({"type": "turn_end"})

    async def serve(self) -> None:
        """Reader loop: dispatch inbound messages until the socket closes."""
        title = ""
        if self._store is not None and self._session_id is not None:
            meta = self._store.get(self._session_id)
            title = meta.title if meta is not None else ""
        await self.ws.send_json(
            {
                "type": "hello",
                "agent": self.profile.name,
                "model": self.profile.llm.model,
                "workspace": str(self.agent.tool_ctx.workspace),
                "session": self._session_id,
                "title": title,
            }
        )
        while True:
            msg = await self.ws.receive_json()
            kind = msg.get("type")
            if kind == "user":
                text = str(msg.get("text", ""))
                if text.strip():
                    # Launch concurrently so confirm replies can still be read.
                    asyncio.create_task(self._run_turn(text))
            elif kind == "confirm_response":
                fut = self._pending_confirm
                if fut is not None and not fut.done():
                    fut.set_result(bool(msg.get("approved")))


def create_app(
    profile_path: str | Path,
    workspace: str | None = None,
    llm_factory: LLMFactory | None = None,
) -> FastAPI:
    """Build the FastAPI app for a given profile.

    The profile and its session store are loaded once; each WebSocket
    connection builds its own Agent (isolated tool options) on top of a stored
    session — resumed when the client names one, fresh otherwise.
    ``llm_factory`` overrides the LLM client per connection (tests inject a
    scripted fake; default builds a real client from the profile).
    """
    profile = AgentProfile.load(profile_path)
    if workspace:
        profile.workspace = workspace
    base_dir = Path.cwd()

    store, notice = open_store(profile)
    if notice:
        print(f"lingchat: {notice}")
    # Sessions attached to a live socket in this process; a second tab asking
    # for one of these is refused so two agents never interleave one transcript.
    attached: set[str] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if store is not None:
                store.close()

    app = FastAPI(title="LingChat", lifespan=lifespan)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, session: str | None = None) -> None:  # pragma: no cover - exercised via TestClient
        await ws.accept()
        sid: str | None = None
        if store is not None:
            # Adopt a well-formed client id (rows are lazy — an id that never
            # spoke has no row, and that's fine); replace a malformed one.
            sid = session if (session and is_session_id(session)) else new_session_id()
            if sid in attached:
                await ws.send_json({"type": "session_busy", "session": sid})
                await ws.close(code=4409)
                return
            attached.add(sid)
        try:
            web_session = WebSession(
                ws, profile, base_dir,
                llm_factory=llm_factory, store=store, session_id=sid,
            )
            await web_session.serve()
        except WebSocketDisconnect:
            pass
        finally:
            if sid is not None:
                attached.discard(sid)

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        if store is None:
            # notice tells the sidebar *why* history is off (None when the
            # profile opted out via sessions.enabled: false).
            return {"enabled": False, "notice": notice, "sessions": []}
        return {
            "enabled": True,
            "notice": None,
            "sessions": [s.model_dump(mode="json") for s in store.list()],
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        meta = store.get(session_id) if store is not None else None
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown session")
        display = [_stored_to_display(m) for m in store.messages(session_id)]
        return {**meta.model_dump(mode="json"), "messages": display}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        if store is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if session_id in attached:
            # A live SessionMemory would lazily re-create the row on its next
            # append — deleting under it would just resurrect a husk.
            raise HTTPException(status_code=409, detail="session is open in a connected tab")
        if not store.delete(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return {"ok": True}

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, body: _RenameBody) -> dict[str, Any]:
        title = body.title.strip()
        if store is None or not title:
            raise HTTPException(
                status_code=404 if store is None else 422,
                detail="unknown session" if store is None else "title must not be empty",
            )
        try:
            meta = store.rename(session_id, title)
        except SessionError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        return meta.model_dump(mode="json")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=_WEB_DIR), name="static")
    return app
