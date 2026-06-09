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
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from lingcore.agent import Agent
from lingcore.config import AgentProfile
from lingcore.events import (
    AgentEvent,
    Error,
    Final,
    SkillActivated,
    TextDelta,
    ToolCallStarted,
    ToolResultEvent,
)

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
        case Final(content):
            return {"type": "final", "text": content}
        case Error(message):
            return {"type": "error", "message": message}
    return {"type": "unknown"}  # pragma: no cover - exhaustive match above


class WebSession:
    """One browser connection: owns an Agent and bridges it to the socket."""

    def __init__(
        self,
        ws: WebSocket,
        profile: AgentProfile,
        base_dir: Path,
        llm_factory: LLMFactory | None = None,
    ) -> None:
        self.ws = ws
        # Per-connection tool_options dict so a future "allow always" stays
        # isolated to this session (mirrors the CLI composition root).
        self._tool_options = dict(profile.tool_options)
        self.agent = Agent.from_profile(
            profile,
            confirm=self.confirm,
            base_dir=base_dir,
            tool_options=self._tool_options,
            llm=llm_factory() if llm_factory is not None else None,
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
        await self.ws.send_json(
            {
                "type": "hello",
                "agent": self.profile.name,
                "model": self.profile.llm.model,
                "workspace": str(self.agent.tool_ctx.workspace),
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

    The profile is loaded once; each WebSocket connection builds its own Agent
    (fresh memory, isolated tool options). ``llm_factory`` overrides the LLM
    client per connection (tests inject a scripted fake; default builds a real
    client from the profile).
    """
    profile = AgentProfile.load(profile_path)
    if workspace:
        profile.workspace = workspace
    base_dir = Path.cwd()

    app = FastAPI(title="LingChat")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:  # pragma: no cover - exercised via TestClient
        await ws.accept()
        session = WebSession(ws, profile, base_dir, llm_factory=llm_factory)
        try:
            await session.serve()
        except WebSocketDisconnect:
            pass

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=_WEB_DIR), name="static")
    return app
