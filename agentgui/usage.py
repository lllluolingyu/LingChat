"""One usage frame shape for every backend, and the turn timing beside it.

Backends report what their agent told them and nothing more. ``cumulative``
says whether ``models`` counters are running totals for the session (Claude and
Codex) or cover a single request (LingCore), so a consumer that bills for spend
diffs them itself instead of guessing; ``scope`` names what the flat display
counters cover. Per-model counters follow each provider's own convention:
``cached``/``cache_write`` are part of ``input`` for Codex and separate from it
for Claude, and ``reasoning`` is part of ``output``. The flat counters say which
convention they follow in ``cache_in_input``.

Turn timing (``timing`` frames) is measured here for backends that cannot
report it. LingCore measures its own turns (``TurnStatsReported``), and a
backend-supplied ``timing`` frame supersedes this clock.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .protocol import Frame, frame


def model_usage(
    model: str,
    *,
    input: int = 0,
    output: int = 0,
    cached: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
) -> dict[str, Any]:
    return {
        "model": model,
        "input": int(input),
        "output": int(output),
        "cached": int(cached),
        "cache_write": int(cache_write),
        "reasoning": int(reasoning),
    }


def usage_frame(
    models: list[dict[str, Any]],
    *,
    scope: str,
    cumulative: bool,
    cache_in_input: bool,
    input: int,
    output: int,
    cached: int = 0,
    cache_write: int = 0,
    cost_usd: float | None = None,
    context_pct: float | None = None,
) -> Frame:
    """Build a ``usage`` frame.

    ``input``/``output``/``cached``/``cache_write``/``cost_usd``/``context_pct``
    are the flat values the browser displays. ``cache_in_input`` says whether
    ``cached``/``cache_write`` are already counted inside ``input`` or are
    prompt tokens beside it. ``cost_usd`` is the agent's own estimate and is
    never authoritative for billing.
    """
    return frame(
        "usage",
        input=input,
        output=output,
        cached=cached,
        cache_write=cache_write,
        cache_in_input=cache_in_input,
        cost_usd=cost_usd,
        context_pct=context_pct,
        scope=scope,
        cumulative=cumulative,
        models=models,
    )


class TurnClock:
    """One turn's wall time, and how much of it the agent spent generating.

    Time with a tool running or an approval open is the agent waiting on the
    world, so ``model_ms`` leaves it out and output tokens over ``model_ms`` is
    the turn's throughput. It still includes each request's time to first
    token, so it reads lower than a provider's pure decode rate.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self._start = time.monotonic()
        self._depth = 0
        self._since = 0.0
        self._waited = 0.0
        self._tools: set[str] = set()

    def hold(self) -> None:
        if self._depth == 0:
            self._since = time.monotonic()
        self._depth += 1

    def release(self) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self._waited += time.monotonic() - self._since

    def observe(self, wire: dict[str, Any]) -> None:
        """Hold while any tool call is unresolved; calls pair by id."""
        ident = wire.get("id")
        if not isinstance(ident, str) or not ident:
            return
        if wire["type"] == "tool_call" and ident not in self._tools:
            self._tools.add(ident)
            self.hold()
        elif wire["type"] == "tool_result" and ident in self._tools:
            self._tools.discard(ident)
            self.release()

    def finish(self) -> dict[str, Any]:
        """Close any span still open (a call that never resolved) and report."""
        end = time.monotonic()
        if self._depth:
            self._waited += end - self._since
            self._depth = 0
        self._tools.clear()
        elapsed = end - self._start
        return timing_frame(
            started_at=self.started_at,
            ended_at=datetime.now(timezone.utc),
            elapsed_ms=round(elapsed * 1000),
            model_ms=round(max(0.0, elapsed - self._waited) * 1000),
        ).to_wire()


def timing_frame(
    *, started_at: datetime, ended_at: datetime, elapsed_ms: int, model_ms: int
) -> Frame:
    """Build a ``timing`` frame: when the turn ran and how long it generated."""
    return frame(
        "timing",
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        elapsed_ms=elapsed_ms,
        model_ms=model_ms,
    )
