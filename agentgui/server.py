# Authentication, attachment and connection lifecycle adapted from LingChat.
# Copyright LingChat contributors; Apache-2.0. See LICENSE and NOTICE.
"""One socket reader, one active turn, one approval registry per connection."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any, cast
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
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from lingcore.media_types import (
    FILE_MAX_BYTES,
    IMAGE_MAX_BYTES,
    MAX_ATTACHMENTS,
    TOTAL_ATTACHMENT_MAX_BYTES,
)
from pydantic import BaseModel, Field, StrictInt

from .attachments import content_disposition, display_turns, validate_attachments
from .backends.base import AgentBackend, UserTurn
from .backends.codex_backend import CodexBackend
from .backends.lingcore_backend import LingCoreBackend, ProfileCache
from .catalog import Catalog
from .diagnostics import doctor, live_models
from .protocol import ApprovalRequest, Decision, Frame, decision, frame
from .store import Autonomy, SessionRecord, Store

BackendFactory = Callable[[SessionRecord, Store, ProfileCache], AgentBackend]
_WEB_DIR = Path(__file__).with_name("web")
# Streaming deltas are persisted in batches instead of one commit per token.
_DELTA_FLUSH_SECONDS = 1.0


class SessionCreate(BaseModel):
    model_id: str
    workspace: str
    # Typed, so an unknown level is a 422 from pydantic before the store is asked.
    autonomy: Autonomy = "ask"


class RenameBody(BaseModel):
    title: str


class ForkBody(BaseModel):
    through_seq: StrictInt | None = Field(default=None, ge=0)


def make_backend(
    rec: SessionRecord, store: Store, profiles: ProfileCache
) -> AgentBackend:
    if rec.backend == "lingcore":
        return LingCoreBackend(store, profiles)
    if rec.backend == "claude":
        try:
            from .backends.claude_backend import ClaudeBackend
        except ModuleNotFoundError as exc:
            if exc.name != "claude_agent_sdk":
                raise
            raise ValueError(
                "Claude support is not installed; install lingchat[claude]"
            ) from None
        return ClaudeBackend(store)
    if rec.backend == "codex":
        return CodexBackend(store)
    raise ValueError(f"unsupported backend {rec.backend}")


class ChatConnection:
    def __init__(
        self, ws: WebSocket, rec: SessionRecord, store: Store, backend: AgentBackend
    ) -> None:
        self.ws, self.session, self.store, self.backend = ws, rec, store, backend
        self.seq: int | None = None
        self.turn_task: asyncio.Task[None] | None = None
        self.pending: dict[str, asyncio.Future[Decision]] = {}
        self.closed = False
        self.stopping = False
        self.forking = False
        self.terminal = False
        self.started = False
        self.transient: list[dict[str, Any]] = []
        self.buffered: dict[str, Any] | None = None
        self.flushed_at = 0.0

    @property
    def busy(self) -> bool:
        return (
            self.forking
            or self.stopping
            or bool(self.turn_task and not self.turn_task.done())
        )

    async def send(
        self,
        value: Frame | ApprovalRequest | dict[str, Any],
        *,
        transcript: bool = False,
    ) -> None:
        wire = value if isinstance(value, dict) else value.to_wire()
        if transcript and self.seq is not None:
            self.persist(wire)
        if transcript and wire["type"] in {
            "error",
            "cancelled",
            "notice",
            "plugin_notice",
        }:
            self.transient.append(wire)
        if self.closed:
            return
        try:
            await self.ws.send_json(wire)
        except (WebSocketDisconnect, OSError, RuntimeError):
            self.closed = True
            raise WebSocketDisconnect() from None

    def persist(self, wire: dict[str, Any]) -> None:
        if wire["type"] in {"text", "thinking"} and wire.keys() == {"type", "delta"}:
            if self.buffered and self.buffered["type"] == wire["type"]:
                self.buffered["delta"] += wire["delta"]
            else:
                self.flush()
                self.buffered = dict(wire)
            if time.monotonic() - self.flushed_at >= _DELTA_FLUSH_SECONDS:
                self.flush()
            return
        self.flush()
        assert self.seq is not None
        self.store.append_frame(self.session.id, self.seq, wire)

    def flush(self) -> None:
        if self.buffered is not None and self.seq is not None:
            self.store.append_frame(self.session.id, self.seq, self.buffered)
        self.buffered = None
        self.flushed_at = time.monotonic()

    async def approve(self, request: ApprovalRequest) -> Decision:
        if self.closed or self.stopping:
            return "deny"
        future: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self.pending[request.id] = future
        try:
            await self.send(request, transcript=True)
            answer = await future
            return answer if answer in request.options else "deny"
        finally:
            self.pending.pop(request.id, None)

    def deny_pending(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_result("deny")

    async def start(self) -> None:
        await self.backend.start(self.session, self.approve)
        self.started = True
        await self.send(
            frame(
                "hello",
                session=self.session.id,
                backend=self.session.backend,
                model=self.session.model_label,
                native_model=self.session.native_model,
                workspace=self.session.workspace,
                autonomy=self.session.autonomy,
                title=self.session.title,
                capabilities=self.backend.capabilities.to_wire(),
                commands=getattr(self.backend, "commands_metadata", lambda: [])(),
                limits={
                    "max_attachments": MAX_ATTACHMENTS,
                    "image_max_bytes": IMAGE_MAX_BYTES,
                    "file_max_bytes": FILE_MAX_BYTES,
                    "total_max_bytes": TOTAL_ATTACHMENT_MAX_BYTES,
                },
            )
        )

    async def finish(self) -> None:
        self.deny_pending()
        self.flush()
        self.backend.reconcile(self.transient)
        await self.send(frame("turn_end"))
        self.seq = None
        self.transient = []

    async def run(self, stream: AsyncIterator[Frame], *, editing: bool = False) -> None:
        try:
            async with aclosing(cast(AsyncGenerator[Frame, None], stream)):
                async for outgoing in stream:
                    if outgoing.type in {"final", "error", "cancelled"}:
                        self.terminal = True
                    await self.send(outgoing, transcript=True)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            self.closed = True
        except Exception as exc:
            await self.send(
                frame("edit_rejected" if editing else "error", message=str(exc)),
                transcript=True,
            )
        finally:
            if not self.stopping and not self.closed:
                await self.finish()

    async def stop(self) -> None:
        task = self.turn_task
        if not task or task.done() or self.terminal:
            if not self.closed:
                await self.send(frame("stop_ignored"))
            return
        self.stopping = True
        self.deny_pending()
        try:
            # Backends drain their terminal response, or cancel and finalize their
            # native checkpoint, before this connection emits the single turn_end.
            frames = await self.backend.stop()
            if not task.done():
                task.cancel()  # includes Stop before run() acquired its checkpoint
            await asyncio.gather(task, return_exceptions=True)
            for outgoing in frames:
                await self.send(outgoing, transcript=True)
            await self.finish()
        finally:
            self.stopping = False

    async def input(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "approval_response":
            ident = msg.get("id")
            pending = self.pending.get(ident) if isinstance(ident, str) else None
            if pending and not pending.done():
                pending.set_result(decision(msg.get("decision")))
            return
        if kind == "stop":
            await self.stop()
            return
        if kind not in {"user", "edit"}:
            await self.send(frame("error", message="unknown client message"))
            return
        if self.busy:
            await self.send(
                frame("edit_rejected", message="wait for or stop the current turn")
                if kind == "edit"
                else frame("turn_busy")
            )
            return
        try:
            text = msg.get("text", "")
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            if kind == "edit":
                seq = msg.get("seq")
                if not self.backend.capabilities.edit_regenerate:
                    raise ValueError("this backend does not support edit/regenerate")
                if type(seq) is not int or seq < 0:
                    raise ValueError("invalid message sequence")
                existing = next(
                    (t for t in self.store.turns(self.session.id) if t["seq"] == seq),
                    None,
                )
                user = (
                    next((f for f in existing["frames"] if f["type"] == "user"), None)
                    if existing
                    else None
                )
                if (
                    not user
                    or user.get("name")
                    or (not text.strip() and not user.get("attachments"))
                ):
                    raise ValueError("select a nonempty, ordinary user message to edit")
                self.seq = seq
                stream = self.backend.edit(seq, text)
            else:
                attachments = validate_attachments(msg.get("attachments"))
                if not text.strip() and not attachments:
                    raise ValueError("a message cannot be empty")
                for attachment in attachments:
                    allowed = (
                        self.backend.capabilities.images
                        if attachment.kind == "image"
                        else self.backend.capabilities.files
                    )
                    if not allowed:
                        raise ValueError(
                            f"{self.session.backend} does not support {attachment.kind} attachments"
                        )
                seq = self.store.next_seq(self.session.id)
                self.store.put_turn(
                    self.session.id,
                    seq,
                    "user",
                    [
                        {
                            "type": "user",
                            "text": text,
                            "attachments": [
                                a.model_dump(exclude={"fallback_text"})
                                for a in attachments
                            ],
                        }
                    ],
                )
                # Separate assistant row keeps replay and message sequence semantics clear.
                self.seq = seq + 1
                self.store.put_turn(self.session.id, self.seq, "assistant", [])
                if self.session.title == "New chat":
                    self.session.title = (text.strip() or "Attachments")[:80]
                    self.store.save(self.session)
                stream = self.backend.run_turn(UserTurn(text, attachments))
            self.terminal = False
            self.transient = []
            self.turn_task = asyncio.create_task(
                self.run(stream, editing=kind == "edit")
            )
        except (ValueError, TypeError) as exc:
            await self.send(
                frame("edit_rejected" if kind == "edit" else "error", message=str(exc))
            )
            if kind == "user":
                await self.send(frame("turn_end"))

    async def serve(self) -> None:
        await self.start()
        while True:
            try:
                msg = await self.ws.receive_json()
            except ValueError:
                await self.send(frame("error", message="invalid JSON message"))
                continue
            if isinstance(msg, dict):
                await self.input(msg)
            else:
                await self.send(frame("error", message="message must be an object"))

    async def close(self) -> None:
        self.closed = True
        self.deny_pending()
        try:
            await self.stop()
        finally:
            task = self.turn_task
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.flush()
            await self.backend.close()


def create_app(
    *,
    config: str | Path | None = None,
    catalog: Catalog | None = None,
    store_path: str | Path | None = None,
    auth_token: str | None = None,
    require_auth: bool = True,
    backend_factory: BackendFactory = make_backend,
) -> FastAPI:
    models_catalog = catalog or Catalog.load(config)
    store = Store(store_path)
    profiles = ProfileCache()
    token = auth_token or secrets.token_urlsafe(32)
    attached: dict[str, ChatConnection | None] = {}
    model_lock = asyncio.Lock()
    models_loaded = False
    model_notice: str | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for conn in list(attached.values()):
                if conn:
                    await conn.close()
            profiles.close()
            store.close()

    app = FastAPI(title="Agent-Chat-GUI", lifespan=lifespan)
    app.state.auth_token = token
    app.state.catalog = models_catalog
    app.state.store = store

    def token_ok(value: str | None) -> bool:
        return not require_auth or bool(value and secrets.compare_digest(value, token))

    async def auth(
        x_agentgui_token: str | None = Header(default=None),
        token_q: str | None = Query(default=None, alias="token"),
    ) -> None:
        if not token_ok(x_agentgui_token or token_q):
            raise HTTPException(401, "invalid or missing token")

    def get(sid: str) -> SessionRecord:
        rec = store.get(sid)
        if not rec:
            raise HTTPException(404, "unknown session")
        return rec

    @app.get("/api/models", dependencies=[Depends(auth)])
    async def models(live: bool = True) -> dict[str, Any]:
        nonlocal models_loaded, model_notice
        if live and not models_loaded:
            async with model_lock:
                if not models_loaded:
                    discovered, model_notice = await live_models()
                    for entry in discovered:
                        models_catalog.entries.setdefault(entry.id, entry)
                    models_loaded = True
        return {
            "models": [entry.to_wire() for entry in models_catalog.entries.values()],
            "notice": model_notice,
            "workspace": str(Path.cwd()),
            "recent_workspaces": list(
                dict.fromkeys(s["workspace"] for s in store.list_sessions())
            )[:20],
        }

    @app.get("/api/doctor", dependencies=[Depends(auth)])
    async def health() -> dict[str, Any]:
        return await doctor(models_catalog)

    @app.get("/api/sessions", dependencies=[Depends(auth)])
    async def sessions() -> dict[str, Any]:
        return {"enabled": True, "sessions": store.list_sessions()}

    @app.post("/api/sessions", dependencies=[Depends(auth)])
    async def create_session(body: SessionCreate) -> dict[str, Any]:
        entry = models_catalog.entries.get(body.model_id)
        if entry is None:
            raise HTTPException(422, "unknown model; select an entry from /api/models")
        try:
            rec = store.create(entry, body.workspace, body.autonomy)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return rec.to_wire()

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def transcript(sid: str) -> dict[str, Any]:
        rec = get(sid)
        return {**rec.to_wire(), "turns": display_turns(store.turns(sid))}

    @app.patch("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def rename(sid: str, body: RenameBody) -> dict[str, Any]:
        rec = get(sid)
        if not body.title.strip():
            raise HTTPException(422, "title must not be empty")
        rec.title = body.title.strip()[:200]
        store.save(rec)
        conn = attached.get(sid)
        if conn:
            conn.session.title = rec.title
        return rec.to_wire()

    @app.delete("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def delete(sid: str) -> dict[str, bool]:
        if sid in attached:
            raise HTTPException(409, "session is open in a connected tab")
        if not store.delete(sid):
            raise HTTPException(404, "unknown session")
        return {"ok": True}

    @app.post("/api/sessions/{sid}/fork", dependencies=[Depends(auth)])
    async def fork(sid: str, body: ForkBody) -> dict[str, Any]:
        rec = get(sid)
        conn = attached.get(sid)
        if sid in attached and (conn is None or conn.busy or not conn.started):
            raise HTTPException(409, "wait for or stop the current turn before forking")
        backend = conn.backend if conn else backend_factory(rec, store, profiles)
        if conn:
            conn.forking = True
        else:
            attached[sid] = None

        async def deny(_request: ApprovalRequest) -> Decision:
            return "deny"

        try:
            if not conn:
                await backend.start(rec, deny)
            if not backend.capabilities.fork:
                raise ValueError("this backend cannot fork sessions")
            return (await backend.fork(body.through_seq)).to_wire()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from None
        finally:
            if conn:
                conn.forking = False
            else:
                try:
                    await backend.close()
                finally:
                    attached.pop(sid, None)

    @app.get(
        "/api/sessions/{sid}/messages/{seq}/attachments/{index}",
        dependencies=[Depends(auth)],
    )
    async def attachment(sid: str, seq: int, index: int) -> Response:
        get(sid)
        for turn in store.turns(sid):
            if turn["seq"] != seq:
                continue
            for msg in turn["frames"]:
                items = msg.get("attachments", [])
                if 0 <= index < len(items) and items[index].get("data"):
                    item = items[index]
                    return Response(
                        base64.b64decode(item["data"], validate=True),
                        media_type="application/octet-stream",
                        headers={
                            "Content-Disposition": content_disposition(
                                item.get("name")
                            ),
                            "X-Content-Type-Options": "nosniff",
                            "Cache-Control": "no-store",
                        },
                    )
        raise HTTPException(404, "attachment not found")

    @app.websocket("/ws")
    async def websocket(
        ws: WebSocket, session: str | None = None, token: str | None = None
    ) -> None:
        # --allow-remote changes binding only; it never disables Origin checks.
        origin = ws.headers.get("origin")
        scheme = "https" if ws.url.scheme in {"https", "wss"} else "http"
        try:
            parsed = urlsplit(origin) if origin is not None else None
            origin_ok = parsed is None or (
                parsed.scheme == scheme and parsed.netloc == ws.headers.get("host")
            )
        except ValueError:
            origin_ok = False
        if not origin_ok:
            await ws.close(code=4403)
            return
        if not token_ok(token or ws.headers.get("x-agentgui-token")):
            await ws.close(code=4401)
            return
        await ws.accept()
        rec = store.get(session) if session else None
        if rec is None:
            await ws.send_json(
                {
                    "type": "error",
                    "message": "Select New chat or an existing session first.",
                }
            )
            await ws.close(code=4404)
            return
        if rec.id in attached:
            await ws.send_json({"type": "session_busy", "session": rec.id})
            await ws.close(code=4409)
            return
        attached[rec.id] = None
        conn: ChatConnection | None = None
        try:
            if not Path(rec.workspace).is_dir():
                raise ValueError("the session workspace no longer exists")
            conn = ChatConnection(ws, rec, store, backend_factory(rec, store, profiles))
            attached[rec.id] = conn
            await conn.serve()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            try:
                await ws.send_json(frame("error", message=str(exc)).to_wire())
                await ws.close(code=4411)
            except (WebSocketDisconnect, RuntimeError):
                pass
        finally:
            try:
                if conn:
                    await conn.close()
            finally:
                attached.pop(rec.id, None)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            _WEB_DIR / "index.html", headers={"Referrer-Policy": "no-referrer"}
        )

    app.mount("/", StaticFiles(directory=_WEB_DIR), name="static")
    return app
