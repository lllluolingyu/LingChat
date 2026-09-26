"""One usage frame shape for every backend.

Backends report what their agent told them and nothing more. ``cumulative``
says whether ``models`` counters are running totals for the session (Claude and
Codex) or cover a single request (LingCore), so a consumer that bills for spend
diffs them itself instead of guessing; ``scope`` names what the flat display
counters cover. Per-model counters follow each provider's own convention:
``cached``/``cache_write`` are part of ``input`` for Codex and separate from it
for Claude, and ``reasoning`` is part of ``output``.
"""

from __future__ import annotations

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
    input: int,
    output: int,
    cached: int = 0,
    cost_usd: float | None = None,
    context_pct: float | None = None,
) -> Frame:
    """Build a ``usage`` frame.

    ``input``/``output``/``cached``/``cost_usd``/``context_pct`` are the flat
    values the browser chip displays; ``cost_usd`` is the agent's own estimate
    and is never authoritative for billing.
    """
    return frame(
        "usage",
        input=input,
        output=output,
        cached=cached,
        cost_usd=cost_usd,
        context_pct=context_pct,
        scope=scope,
        cumulative=cumulative,
        models=models,
    )
