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
under ``/api/sessions`` serve the sidebar: list, transcript + durable runtime
events, cursor-based event replay, atomic prefix fork, rename, and delete.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
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
    TurnCancelled,
)
from lingcore.media import attachment_from_wire
from lingcore.media_types import (
    FILE_MAX_BYTES,
    IMAGE_MAX_BYTES,
    MAX_ATTACHMENTS,
    TOTAL_ATTACHMENT_MAX_BYTES,
    decoded_payload_size,
    sanitize_name,
)
from lingcore.message import Attachment, Message, UserInput
from lingcore.sessions import (
    SessionEvent,
    SessionStore,
    is_session_id,
    new_session_id,
    open_store,
)
from lingcore.tools.builtin.shell import allowlist_pattern_for
from pydantic import BaseModel, Field, StrictInt


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


def _attachment_payloads(
    attachments: list[Attachment], *, downloadable: bool = False
) -> list[dict[str, Any]]:
    """Return safe browser metadata for attachments.

    ``fallback_text`` is always model-only. Images retain their validated base64
    bytes for inline previews; every other kind omits ``data`` and is fetched as
    an authenticated download only after it has a stored message sequence.
    """
    payloads: list[dict[str, Any]] = []
    for attachment in attachments:
        excluded = {"fallback_text"}
        if attachment.kind != "image":
            excluded.add("data")
        payload = attachment.model_dump(exclude=excluded)
        payload["size"] = decoded_payload_size(attachment.data)
        payload["download"] = downloadable
        payloads.append(payload)
    return payloads


def _validate_attachments(raw: object) -> list[Attachment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments must be a list")
    if len(raw) > MAX_ATTACHMENTS:
        raise ValueError(f"too many attachments ({len(raw)}; limit {MAX_ATTACHMENTS})")
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
            # call.id / call_id let the browser pair a result with its exact
            # card — pairing by name misroutes parallel calls to the same tool.
            return {
                "type": "tool_call",
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
        case ToolResultEvent(result):
            return {
                "type": "tool_result",
                "id": result.call_id,
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
        case TurnCancelled(reason):
            return {"type": "cancelled", "reason": reason}
        case Final(content):
            return {"type": "final", "text": content}
        case Error(message):
            return {"type": "error", "message": message}
    return {"type": "unknown"}  # pragma: no cover - exhaustive match above


def _stored_to_display(seq: int, m: Message) -> dict[str, Any]:
    """Map one stored message to the shape the transcript endpoint serves.

    ``ToolResult.ok`` is not stored on ``Message``; the loop encodes failures
    as an ``"ERROR: "`` content prefix (its own convention), which is what
    ``ok`` reflects here.
    """
    if m.role == "user":
        return {
            "seq": seq,
            "role": "user",
            "text": m.input_text if m.input_text is not None else m.content,
            "name": m.name,
            "attachments": _attachment_payloads(m.attachments, downloadable=True),
        }
    if m.role == "assistant":
        return {
            "seq": seq,
            "role": "assistant",
            "text": m.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in m.tool_calls
            ],
        }
    return {
        "seq": seq,
        "role": "tool",
        "id": m.tool_call_id,
        "name": m.name,
        "ok": not m.content.startswith("ERROR: "),
        "content": m.content,
    }


def _stored_event_to_display(event: SessionEvent) -> dict[str, Any] | None:
    """Map a durable LingCore runtime event to the replay wire contract.

    Events are derived state, so a malformed payload is omitted rather than
    making the canonical transcript endpoint fail. ``event_seq`` remains a
    monotonic cursor even when an omitted or rewound event leaves a gap.
    """
    base = {
        "event_seq": event.event_seq,
        "message_seq": event.message_seq,
        "created_at": event.created_at.isoformat(),
    }
    if event.kind == "compaction":
        keys = (
            "summarized_messages",
            "before_tokens",
            "after_tokens",
        )
        values = [event.payload.get(key) for key in keys]
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in values
        ):
            return None
        return {
            **base,
            "type": "compact",
            **dict(zip(keys, values, strict=True)),
        }
    if event.kind == "skill_state":
        state: dict[str, list[str]] = {}
        for key in ("active", "activated", "deactivated"):
            value = event.payload.get(key)
            if not isinstance(value, list) or not all(
                isinstance(name, str) and name for name in value
            ):
                return None
            state[key] = list(dict.fromkeys(value))
        return {**base, "type": "skill_state", **state}
    return None


def _replay_events(
    store: SessionStore, session_id: str, *, after: int = -1
) -> list[dict[str, Any]]:
    display: list[dict[str, Any]] = []
    for event in store.events(session_id, after_seq=after):
        mapped = _stored_event_to_display(event)
        if mapped is not None:
            display.append(mapped)
    return display


class _RenameBody(BaseModel):
    title: str


class _ForkBody(BaseModel):
    through_seq: StrictInt | None = Field(default=None, ge=0)
    title: str | None = None


@dataclass(slots=True)
class _PendingConfirm:
    future: asyncio.Future[bool]
    command: str


def shell_runner_label(tool_options: Mapping[str, Any]) -> str:
    """Describe the selected runner from raw, cross-version profile options.

    LingCore 0.2.x does not expose ``lingcore.sandbox``, so LingChat deliberately
    avoids importing sandbox models and reads only the stable options mapping.
    """
    run_shell = tool_options.get("run_shell")
    if not isinstance(run_shell, Mapping):
        return "host (unsandboxed)"
    sandbox = run_shell.get("sandbox")
    if not isinstance(sandbox, Mapping):
        return "host (unsandboxed)"
    backend = sandbox.get("backend")
    if backend == "bubblewrap":
        return "bubblewrap"
    if backend == "oci":
        runtime = sandbox.get("runtime")
        return f"oci: {runtime}" if runtime in {"docker", "podman"} else "oci"
    return "host (unsandboxed)"


def _content_disposition(name: str | None) -> str:
    safe = sanitize_name(name, fallback="attachment")
    fallback = safe.encode("ascii", errors="replace").decode("ascii")
    fallback = fallback.replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(safe, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


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
        self.profile = profile
        self._base_dir = base_dir
        self._llm_factory = llm_factory
        # Per-connection mutable options: nested run_shell allowlists must never
        # leak through the shared profile into another browser connection.
        # Copy only here, before Agent.from_profile injects live non-copyable
        # skill state and memory summarizers into this dict.
        self._tool_options = copy.deepcopy(profile.tool_options)
        self._store = store
        self._session_id = session_id
        self.agent = self._build_agent()
        # One future per in-flight confirmation, keyed by a generated id, so two
        # simultaneous tool calls (parallel_tools) each get their own round-trip
        # and an approval is never misrouted to the wrong command.
        self._pending_confirms: dict[str, _PendingConfirm] = {}
        self._turn_shell_commands: set[str] = set()
        self._run_lock = asyncio.Lock()
        # Exactly one turn may be active. This makes Stop deterministic and
        # rejects accidental double-submits instead of silently queueing them.
        self._tasks: set[asyncio.Task[None]] = set()
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_terminal = False

    def _build_agent(self, *, llm: Any = None) -> Agent:
        client = llm
        if client is None and self._llm_factory is not None:
            client = self._llm_factory()
        return Agent.from_profile(
            self.profile,
            confirm=self.confirm,
            base_dir=self._base_dir,
            tool_options=self._tool_options,
            llm=client,
            session_store=self._store,
            session_id=self._session_id,
        )

    async def confirm(self, command: str) -> bool:
        """Ask the browser to approve a command; await its click.

        Each call gets a unique id echoed back in the ``confirm_response`` so
        concurrent confirmations don't clobber one another's future.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        cid = uuid.uuid4().hex
        self._pending_confirms[cid] = _PendingConfirm(future=fut, command=command)
        pattern = self._allowlist_pattern(command)
        message: dict[str, Any] = {
            "type": "confirm",
            "id": cid,
            "command": command,
            "runner": shell_runner_label(self._tool_options),
        }
        if pattern is not None:
            message["allowlist_pattern"] = pattern
        await self.ws.send_json(message)
        try:
            return await fut
        finally:
            self._pending_confirms.pop(cid, None)

    def _allowlist_pattern(self, command: str) -> str | None:
        if command not in self._turn_shell_commands:
            return None
        pattern = allowlist_pattern_for(command)
        return pattern or None

    def _pending_confirm(self, cid: str | None) -> _PendingConfirm | None:
        """Find a prompt by id, retaining the legacy sole-prompt fallback."""
        if cid is None:
            if len(self._pending_confirms) != 1:
                return None
            cid = next(iter(self._pending_confirms))
        return self._pending_confirms.get(cid)

    async def _resolve_confirm(
        self, cid: str | None, approved: bool, scope: object = "once"
    ) -> None:
        """Resolve a pending confirmation by id (or the sole one if no id)."""
        pending = self._pending_confirm(cid)
        if pending is None or pending.future.done():
            return
        if approved and scope == "session":
            pattern = self._allowlist_pattern(pending.command)
            added = False
            if pattern is not None:
                raw_options = self._tool_options.get("run_shell")
                if not isinstance(raw_options, dict):
                    raw_options = {}
                    self._tool_options["run_shell"] = raw_options
                patterns = raw_options.get("allow_patterns")
                if not isinstance(patterns, list):
                    patterns = []
                    raw_options["allow_patterns"] = patterns
                if pattern not in patterns:
                    patterns.append(pattern)
                    added = True
            await self._safe_send(
                {"type": "shell_allowlist", "pattern": pattern, "added": added}
            )
        pending.future.set_result(approved)

    def spawn_turn(self, incoming: UserInput) -> bool:
        """Launch one turn, refusing to queue behind an active turn."""
        if self._turn_task is not None and not self._turn_task.done():
            return False
        task = asyncio.create_task(self._run_turn(incoming))
        self._turn_task = task
        self._turn_terminal = False
        self._tasks.add(task)
        task.add_done_callback(self._turn_done)
        return True

    def _turn_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if self._turn_task is task:
            self._turn_task = None
            self._turn_terminal = False

    async def stop_turn(self) -> bool:
        """Stop the active turn, repair its history, and notify the browser."""
        task = self._turn_task
        if task is None or task.done() or self._turn_terminal:
            await self._safe_send({"type": "stop_ignored"})
            return False
        agent_turn_started = self.agent.cancel_turn()
        if not agent_turn_started:
            task.cancel()  # the task may not have entered Agent.run yet
        await asyncio.gather(task, return_exceptions=True)
        if not task.cancelled():
            return False
        # cancel_turn() succeeds only after Agent acquired its checkpoint. An
        # immediate Stop can cancel this wrapper before that point; there is no
        # Agent state to finalize, but it is still a successful UI cancellation.
        event: AgentEvent = (
            self.agent.finalize_cancelled_turn()
            if agent_turn_started
            else TurnCancelled()
        )
        await self._safe_send(_event_to_msg(event))
        await self._safe_send({"type": "turn_end"})
        return True

    async def aclose(self) -> None:
        """Tear down on disconnect: cancel in-flight turns and await them.

        A long-running tool (e.g. run_shell) is cancelled so it stops touching
        the shared session before the lease is released — otherwise a second tab
        could attach and a second agent interleave writes on the same transcript.
        """
        tasks = list(self._tasks)
        agent_turn_started = self.agent.cancel_turn()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if agent_turn_started and any(task.cancelled() for task in tasks):
            self.agent.finalize_cancelled_turn()

    async def _run_turn(self, incoming: UserInput) -> None:
        # Serialize turns so one connection's runs share memory safely.
        async with self._run_lock:
            self._turn_shell_commands.clear()
            turn = self.agent.run(incoming)
            try:
                # A failure in the loop body does not make ``async for`` close
                # its generator immediately. Own it explicitly so a failed
                # outbound send rolls back the abandoned Agent turn before this
                # task exits, rather than leaving cleanup to asyncgen GC.
                async with aclosing(turn):
                    async for event in turn:
                        if (
                            isinstance(event, ToolCallStarted)
                            and event.call.name == "run_shell"
                        ):
                            command = event.call.arguments.get("command")
                            if isinstance(command, str):
                                self._turn_shell_commands.add(command)
                        if isinstance(event, (Final, Error)):
                            self._turn_terminal = True
                        await self.ws.send_json(_event_to_msg(event))
            except WebSocketDisconnect:
                # The owned stream has already repaired any non-terminal turn.
                # The reader loop will observe the same closed socket; do not
                # leave an unobserved exception on this background task.
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one turn kill the connection
                self._turn_terminal = True
                await self._safe_send(
                    {"type": "error", "message": f"internal error: {e!r}"}
                )
            await self._safe_send({"type": "turn_end"})

    async def _edit_turn(self, seq: object, text: str) -> None:
        """Rewind to a stored user message and regenerate from edited text."""
        if self._turn_task is not None and not self._turn_task.done():
            await self._safe_send(
                {
                    "type": "edit_rejected",
                    "message": "wait for or stop the active turn first",
                }
            )
            return
        if self._store is None or self._session_id is None:
            await self._safe_send(
                {"type": "edit_rejected", "message": "editing requires session history"}
            )
            return
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            await self._safe_send(
                {"type": "edit_rejected", "message": "invalid message sequence"}
            )
            return
        try:
            record = next(
                (
                    record
                    for record in self._store.message_records(self._session_id)
                    if record.seq == seq
                ),
                None,
            )
            if record is None:
                raise SessionError(f"no message {seq} in session {self._session_id!r}")
            candidate = record.message
            if candidate.role != "user" or candidate.name is not None:
                raise SessionError("only an ordinary user message can be edited")
            if not text.strip() and not candidate.attachments:
                await self._safe_send(
                    {
                        "type": "edit_rejected",
                        "message": "an edited message cannot be empty",
                    }
                )
                return
            original = self._store.rewind_to_user_message(self._session_id, seq)
        except SessionError as exc:
            await self._safe_send({"type": "edit_rejected", "message": str(exc)})
            return

        # Rehydrate from the surviving branch while reusing the same model
        # client and per-session tool options. LingCore restores any surviving
        # compaction snapshot and dynamic-skill state during this rebuild.
        self.agent = self._build_agent(llm=self.agent.llm)
        await self._safe_send({"type": "edit_accepted", "seq": seq, "text": text})
        incoming = UserInput(text=text, attachments=original.attachments)
        if not self.spawn_turn(incoming):  # defensive; reader is serialized
            await self._safe_send(
                {"type": "edit_rejected", "message": "another turn started first"}
            )

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
                "limits": {
                    "max_attachments": MAX_ATTACHMENTS,
                    "image_max_bytes": IMAGE_MAX_BYTES,
                    "file_max_bytes": FILE_MAX_BYTES,
                    "total_max_bytes": TOTAL_ATTACHMENT_MAX_BYTES,
                },
                "event_cursor": (
                    self._store.event_cursor(self._session_id)
                    if self._store is not None and self._session_id is not None
                    else -1
                ),
            }
        )
        while True:
            msg = await self.ws.receive_json()
            if not isinstance(msg, dict):
                await self.ws.send_json(
                    {"type": "error", "message": "invalid protocol message"}
                )
                continue
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
                    await self.ws.send_json(
                        {"type": "error", "message": f"attachment error: {e}"}
                    )
                    await self.ws.send_json({"type": "turn_end"})
                    continue
                if incoming is not None:
                    # Launch as a tracked task so confirm replies can still be
                    # read concurrently and a disconnect can cancel it cleanly.
                    if not self.spawn_turn(incoming):
                        await self.ws.send_json({"type": "turn_busy"})
            elif kind == "stop":
                await self.stop_turn()
            elif kind == "edit":
                await self._edit_turn(msg.get("seq"), str(msg.get("text", "")))
            elif kind == "confirm_response":
                raw_id = msg.get("id")
                if raw_id is not None and not isinstance(raw_id, str):
                    continue
                # Confirmation is a security boundary: only the literal JSON
                # boolean true approves. Strings/numbers/missing values fail
                # closed instead of inheriting Python truthiness.
                await self._resolve_confirm(
                    raw_id,
                    msg.get("approved") is True,
                    msg.get("scope", "once"),
                )


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
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if store is not None:
                store.close()

    app = FastAPI(title="LingChat", lifespan=lifespan)
    app.state.auth_token = token
    app.state.profile = profile

    async def _require_token(
        x_lingchat_token: str | None = Header(default=None),
        token_q: str | None = Query(default=None, alias="token"),
    ) -> None:
        if not _token_ok(x_lingchat_token or token_q):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.websocket("/ws")
    async def ws_endpoint(
        ws: WebSocket, session: str | None = None, token: str | None = None
    ) -> None:  # pragma: no cover - exercised via TestClient
        # Authenticate BEFORE accepting: reject a bad origin or missing token at
        # the handshake so an unauthorized page never opens the socket.
        page_scheme = "https" if ws.url.scheme in ("wss", "https") else "http"
        if not _origin_ok(
            ws.headers.get("origin"), ws.headers.get("host"), page_scheme
        ):
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
                ws,
                profile,
                base_dir,
                llm_factory=llm_factory,
                store=store,
                session_id=sid,
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
    async def get_session(
        session_id: str, _: None = Depends(_require_token)
    ) -> dict[str, Any]:
        if store is None:
            raise HTTPException(status_code=404, detail="unknown session")
        meta = store.get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown session")
        display = [
            _stored_to_display(record.seq, record.message)
            for record in store.message_records(session_id)
        ]
        return {
            **meta.model_dump(mode="json"),
            "messages": display,
            "events": _replay_events(store, session_id),
            "event_cursor": store.event_cursor(session_id),
        }

    @app.get("/api/sessions/{session_id}/messages/{seq}/attachments/{index}")
    async def download_attachment(
        session_id: str,
        seq: int,
        index: int = PathParam(ge=0),
        _: None = Depends(_require_token),
    ) -> Response:
        if store is None or store.get(session_id) is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        record = next(
            (item for item in store.message_records(session_id) if item.seq == seq),
            None,
        )
        if record is None or index >= len(record.message.attachments):
            raise HTTPException(status_code=404, detail="attachment not found")
        attachment = record.message.attachments[index]
        try:
            data = base64.b64decode(attachment.data, validate=True)
        except ValueError:
            raise HTTPException(
                status_code=404, detail="attachment not found"
            ) from None
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": _content_disposition(attachment.name),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/sessions/{session_id}/events")
    async def replay_session_events(
        session_id: str,
        after: int = Query(default=-1, ge=-1),
        _: None = Depends(_require_token),
    ) -> dict[str, Any]:
        if store is None:
            raise HTTPException(status_code=404, detail="unknown session")
        meta = store.get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return {
            "session": session_id,
            "events": _replay_events(store, session_id, after=after),
            # Never move a caller's cursor backwards when Edit removed the
            # branch containing its last event. AUTOINCREMENT guarantees the
            # next replacement event will still compare greater than ``after``.
            "cursor": max(after, store.event_cursor(session_id)),
        }

    @app.post("/api/sessions/{session_id}/fork")
    async def fork_session(
        session_id: str,
        body: _ForkBody,
        _: None = Depends(_require_token),
    ) -> dict[str, Any]:
        if store is None or store.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        title: str | None = None
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(status_code=422, detail="title must not be empty")
        try:
            forked = store.fork_session(
                session_id,
                through_seq=body.through_seq,
                title=title,
            )
        except SessionError as exc:
            # The source exists, so remaining failures describe a boundary that
            # cannot form a valid branch (missing seq, incomplete tool block,
            # empty/corrupt source). Nothing was copied: the core operation is
            # transactional.
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return forked.model_dump(mode="json")

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(
        session_id: str, _: None = Depends(_require_token)
    ) -> dict[str, Any]:
        if store is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if session_id in attached:
            # A live SessionMemory would lazily re-create the row on its next
            # append — deleting under it would just resurrect a husk.
            raise HTTPException(
                status_code=409, detail="session is open in a connected tab"
            )
        if not store.delete(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return {"ok": True}

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(
        session_id: str, body: _RenameBody, _: None = Depends(_require_token)
    ) -> dict[str, Any]:
        title = body.title.strip()
        if store is None or not title:
            raise HTTPException(
                status_code=404 if store is None else 422,
                detail="unknown session"
                if store is None
                else "title must not be empty",
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
