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
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from lingcore.agent import Agent
from lingcore.config import AgentProfile
from lingcore.errors import SessionError
from lingcore.events import (
    AgentEvent,
    Compacted,
    Error,
    Final,
    SkillActivated,
    StreamRetry,
    TextDelta,
    ToolCallStarted,
    ToolResultEvent,
)
from lingcore.message import Attachment, Message, UserInput
from lingcore.media import attachment_from_wire
from lingcore.sessions import SessionStore, is_session_id, new_session_id, open_store

def _find_web_dir() -> Path:
    """Locate the static UI: ``lingchat/web`` in an installed wheel (the build
    force-includes it there), ``../web`` in a repo checkout."""
    pkg_local = Path(__file__).resolve().parent / "web"
    if pkg_local.is_dir():
        return pkg_local
    return Path(__file__).resolve().parent.parent / "web"


_WEB_DIR = _find_web_dir()

# An optional zero-arg factory returning an LLMClient-shaped object. When given,
# each session uses it instead of building a real client — the seam tests use to
# drive the bridge with a scripted fake, and embedders can use to inject a
# custom backend.
LLMFactory = Callable[[], Any]
_MAX_ATTACHMENTS = 4


def _attachment_payloads(attachments: list[Attachment]) -> list[dict[str, Any]]:
    # fallback_text is a model-facing stand-in (extracted PDF text / a vision
    # description, up to tens of KB) — the browser renders the original media.
    return [a.model_dump(exclude={"fallback_text"}) for a in attachments]


def _validate_attachments(raw: object) -> list[Attachment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments must be a list")
    if len(raw) > _MAX_ATTACHMENTS:
        raise ValueError(f"too many attachments ({len(raw)}; limit {_MAX_ATTACHMENTS})")
    out: list[Attachment] = []
    for item in raw:
        try:
            out.append(attachment_from_wire(item))
        except Exception as e:
            raise ValueError(str(e)) from None
    return out


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
                "attachments": _attachment_payloads(result.attachments),
            }
        case SkillActivated(name, active):
            return {"type": "skill", "name": name, "active": active}
        case Compacted(summarized_messages, before_tokens, after_tokens):
            return {
                "type": "compact",
                "summarized_messages": summarized_messages,
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
            }
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
        return {
            "role": "user",
            "text": m.content,
            "name": m.name,
            "attachments": _attachment_payloads(m.attachments),
        }
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
        # One future per in-flight confirmation, keyed by a generated id, so two
        # simultaneous tool calls (parallel_tools) each get their own round-trip
        # and an approval is never misrouted to the wrong command.
        self._pending_confirms: dict[str, asyncio.Future[bool]] = {}
        self._run_lock = asyncio.Lock()
        # In-flight turn tasks, tracked so a disconnect can cancel and await them
        # before the session lease is released (no orphaned agent on the store).
        self._tasks: set[asyncio.Task[None]] = set()

    async def confirm(self, command: str) -> bool:
        """Ask the browser to approve a command; await its click.

        Each call gets a unique id echoed back in the ``confirm_response`` so
        concurrent confirmations don't clobber one another's future.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        cid = uuid.uuid4().hex
        self._pending_confirms[cid] = fut
        await self.ws.send_json({"type": "confirm", "id": cid, "command": command})
        try:
            return await fut
        finally:
            self._pending_confirms.pop(cid, None)

    def _resolve_confirm(self, cid: str | None, approved: bool) -> None:
        """Resolve a pending confirmation by id (or the sole one if no id)."""
        if cid is None:
            # Back-compat / single-prompt case: resolve the only pending confirm.
            if len(self._pending_confirms) == 1:
                cid = next(iter(self._pending_confirms))
            else:
                return
        fut = self._pending_confirms.get(cid)
        if fut is not None and not fut.done():
            fut.set_result(approved)

    def spawn_turn(self, incoming: UserInput) -> None:
        """Launch a turn as a tracked task (so disconnect can cancel it)."""
        task = asyncio.create_task(self._run_turn(incoming))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Tear down on disconnect: cancel in-flight turns and await them.

        A long-running tool (e.g. run_shell) is cancelled so it stops touching
        the shared session before the lease is released — otherwise a second tab
        could attach and a second agent interleave writes on the same transcript.
        """
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_turn(self, incoming: UserInput) -> None:
        # Serialize turns so one connection's runs share memory safely.
        async with self._run_lock:
            try:
                async for event in self.agent.run(incoming):
                    await self.ws.send_json(_event_to_msg(event))
            except (WebSocketDisconnect, asyncio.CancelledError):
                raise
            except Exception as e:  # never let one turn kill the connection
                await self._safe_send({"type": "error", "message": f"internal error: {e!r}"})
            await self._safe_send({"type": "turn_end"})

    async def _safe_send(self, msg: dict[str, Any]) -> None:
        """Send, tolerating a socket that closed underneath us."""
        try:
            await self.ws.send_json(msg)
        except Exception:
            pass

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
                try:
                    attachments = _validate_attachments(msg.get("attachments"))
                    # UserInput re-validates the *aggregate* (count + total
                    # decoded size); pydantic's ValidationError subclasses
                    # ValueError, so an over-limit batch is refused here as a
                    # per-turn error instead of killing the socket.
                    incoming = (
                        UserInput(text=text, attachments=attachments)
                        if text.strip() or attachments
                        else None
                    )
                except ValueError as e:
                    await self.ws.send_json({"type": "error", "message": f"attachment error: {e}"})
                    continue
                if incoming is not None:
                    # Launch as a tracked task so confirm replies can still be
                    # read concurrently and a disconnect can cancel it cleanly.
                    self.spawn_turn(incoming)
            elif kind == "confirm_response":
                self._resolve_confirm(msg.get("id"), bool(msg.get("approved")))


def create_app(
    profile_path: str | Path,
    workspace: str | None = None,
    llm_factory: LLMFactory | None = None,
    *,
    require_auth: bool = True,
    auth_token: str | None = None,
    allowed_origins: list[str] | None = None,
) -> FastAPI:
    """Build the FastAPI app for a given profile.

    The profile and its session store are loaded once; each WebSocket
    connection builds its own Agent (isolated tool options) on top of a stored
    session — resumed when the client names one, fresh otherwise.
    ``llm_factory`` overrides the LLM client per connection (tests inject a
    scripted fake; default builds a real client from the profile).

    Authentication boundary (the agent can run shell — treat the port as
    remote code execution): when ``require_auth`` is true (the default) every
    ``/ws`` and ``/api`` request must present a high-entropy per-launch token
    (``auth_token`` or an auto-generated one, exposed as ``app.state.auth_token``
    and printed by the CLI) via the ``token`` query param or ``X-LingChat-Token``
    header, and a WebSocket carrying an ``Origin`` header must match the server's
    own origin (blocking cross-site WebSocket hijacking from a malicious page).
    Tests pass ``require_auth=False`` to exercise the bridge without the gate.
    """
    profile = AgentProfile.load(profile_path)
    if workspace:
        profile.workspace = workspace
    base_dir = Path.cwd()

    token: str | None = None
    if require_auth:
        token = auth_token or secrets.token_urlsafe(32)

    def _token_ok(provided: str | None) -> bool:
        if not require_auth:
            return True
        return bool(provided) and secrets.compare_digest(provided or "", token or "")

    def _origin_ok(origin: str | None, host_header: str | None, scheme: str) -> bool:
        """Accept a WebSocket handshake's Origin.

        No Origin (a non-browser client) is allowed — the token still gates it.
        A browser always sends Origin; it must match the server's own origin —
        scheme AND authority (or an explicit ``allowed_origins`` entry) — so a
        page on another origin cannot open the socket even from loopback.
        Comparing the netloc alone is not enough: ``https://host`` and
        ``http://host`` are different origins, and a cross-scheme page must be
        refused like any other foreign one. ``scheme`` is the page scheme the
        connection implies ("https" for a wss/https request, else "http").
        """
        if origin is None:
            return True
        if allowed_origins is not None:
            return origin in allowed_origins
        if not host_header:
            return False
        parsed = urlsplit(origin)
        return parsed.scheme == scheme and parsed.netloc == host_header

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
    app.state.auth_token = token

    async def _require_token(
        x_lingchat_token: str | None = Header(default=None),
        token_q: str | None = Query(default=None, alias="token"),
    ) -> None:
        if not _token_ok(x_lingchat_token or token_q):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, session: str | None = None, token: str | None = None) -> None:  # pragma: no cover - exercised via TestClient
        # Authenticate BEFORE accepting: reject a bad origin or missing token at
        # the handshake so an unauthorized page never opens the socket.
        page_scheme = "https" if ws.url.scheme in ("wss", "https") else "http"
        if not _origin_ok(ws.headers.get("origin"), ws.headers.get("host"), page_scheme):
            await ws.close(code=4403)
            return
        if not _token_ok(token or ws.headers.get("x-lingchat-token")):
            await ws.close(code=4401)
            return
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
        web_session: WebSession | None = None
        try:
            web_session = WebSession(
                ws, profile, base_dir,
                llm_factory=llm_factory, store=store, session_id=sid,
            )
            await web_session.serve()
        except WebSocketDisconnect:
            pass
        finally:
            # Cancel and await in-flight turns BEFORE releasing the lease, so no
            # detached agent keeps writing to a session a new tab could adopt.
            if web_session is not None:
                await web_session.aclose()
            if sid is not None:
                attached.discard(sid)

    @app.get("/api/sessions")
    async def list_sessions(_: None = Depends(_require_token)) -> dict[str, Any]:
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
    async def get_session(session_id: str, _: None = Depends(_require_token)) -> dict[str, Any]:
        meta = store.get(session_id) if store is not None else None
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown session")
        display = [_stored_to_display(m) for m in store.messages(session_id)]
        return {**meta.model_dump(mode="json"), "messages": display}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, _: None = Depends(_require_token)) -> dict[str, Any]:
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
    async def rename_session(session_id: str, body: _RenameBody, _: None = Depends(_require_token)) -> dict[str, Any]:
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
