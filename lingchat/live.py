"""A replaceable viewer for work owned by the server, with atomic live replay."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from lingcore.background import BackgroundInbox, BackgroundInput


async def pump_background(
    current: Callable[[], BackgroundInbox | None],
    idle: asyncio.Event,
    deliver: Callable[[BackgroundInput], Awaitable[None]],
) -> None:
    """Serialize monitor turns with user turns and survive Agent replacement."""
    while (inbox := current()) is not None:
        await inbox.wait()
        # Batch bursts and give an arriving user message the first turn.
        await asyncio.sleep(0.1)
        await idle.wait()
        if inbox is not current():
            continue
        if inbox.closed:
            return
        incoming = inbox.take()
        if incoming is not None:
            await deliver(incoming)


class LiveChannel:
    def __init__(self, ws: WebSocket) -> None:
        self.ws: WebSocket | None = ws
        self.viewer: WebSocket | None = ws
        self.lock = asyncio.Lock()
        self.history: dict[str, Any] | None = None
        self.frames: list[dict[str, Any]] = []

    def begin(self, history: dict[str, Any]) -> None:
        self.history = history
        self.frames = []

    def detach(self, ws: WebSocket) -> None:
        # A late disconnect from an old socket must not detach its replacement.
        if self.ws is ws:
            self.ws = None
        if self.viewer is ws:
            self.viewer = None

    async def _send(self, wire: dict[str, Any]) -> None:
        ws = self.ws
        if ws is None:
            return
        try:
            # A viewer which stops reading must not stall the agent indefinitely.
            async with asyncio.timeout(5):
                await ws.send_json(wire)
        except (WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
            self.detach(ws)

    async def attach(
        self,
        ws: WebSocket,
        hello: Callable[[], dict[str, Any]],
        history: Callable[[], dict[str, Any]],
        approvals: Callable[[], list[dict[str, Any]]],
    ) -> None:
        async with self.lock:
            self.ws = ws
            self.viewer = ws
            # Snapshot and subsequent sends share this lock: no missing or
            # duplicated deltas between history replay and the live stream.
            await self._send(
                {
                    **hello(),
                    "history": self.history if self.history is not None else history(),
                    "replay": list(self.frames),
                    "running": self.history is not None,
                    "approvals": approvals(),
                }
            )

    async def send(
        self,
        wire: dict[str, Any],
        *,
        record: Callable[[dict[str, Any]], None] | None = None,
        replay: bool = False,
        end_turn: bool = False,
    ) -> None:
        async with self.lock:
            if record is not None:
                record(wire)
            if replay and self.history is not None:
                # Keep only one turn's stream, coalescing token-sized deltas.
                key = "delta" if "delta" in wire else "text"
                if (
                    wire["type"] in {"text", "thinking"}
                    and wire.keys() == {"type", key}
                    and self.frames
                    and self.frames[-1].keys() == wire.keys()
                    and self.frames[-1]["type"] == wire["type"]
                ):
                    self.frames[-1][key] += wire[key]
                else:
                    self.frames.append(dict(wire))
            if end_turn:
                self.history = None
                self.frames = []
            await self._send(wire)
