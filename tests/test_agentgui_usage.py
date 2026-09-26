"""Every backend reports usage in one frame shape a biller can diff."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lingcore.llm import LLMChunk
from lingcore.usage import TokenUsage

from agentgui.backends.base import UserTurn
from agentgui.backends.claude_backend import ClaudeBackend
from agentgui.backends.codex_backend import CodexBackend
from agentgui.backends.lingcore_backend import LingCoreBackend, ProfileCache
from agentgui.catalog import ModelEntry
from agentgui.store import Store

FAKE_CLAUDE = str(
    Path(__file__).parent.joinpath("agentgui_fakes/fake_claude").resolve()
)
FAKE_CODEX = [
    "python3",
    str(Path(__file__).parent.joinpath("agentgui_fakes/fake_codex_appserver.py")),
]


async def deny(_request):
    return "deny"


def _usage(frames: list[dict]) -> dict:
    return next(f for f in frames if f["type"] == "usage")


async def test_claude_usage_carries_cumulative_per_model_totals(tmp_path) -> None:
    workspace = tmp_path / "w"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    rec = store.create(ModelEntry("c", "Claude", "claude", "sonnet"), str(workspace))
    backend = ClaudeBackend(store, FAKE_CLAUDE)
    try:
        await asyncio.wait_for(backend.start(rec, deny), 5)
        async with asyncio.timeout(8):
            frames = [f.to_wire() async for f in backend.run_turn(UserTurn("hi"))]
    finally:
        await backend.close()
        store.close()
    usage = _usage(frames)
    # Flat chip values stay the turn's own main-loop counters.
    assert (usage["input"], usage["output"], usage["cached"]) == (12, 3, 4)
    assert usage["cost_usd"] == 0.001
    assert usage["scope"] == "turn" and usage["cumulative"] is True
    # models comes from modelUsage: session-cumulative and subagent-inclusive.
    assert usage["models"] == [
        {
            "model": "claude-sonnet-4-9",
            "input": 30,
            "output": 8,
            "cached": 4,
            "cache_write": 6,
            "reasoning": 0,
        }
    ]


async def test_codex_usage_is_thread_cumulative_with_served_model(tmp_path) -> None:
    workspace = tmp_path / "w"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    rec = store.create(ModelEntry("x", "Codex", "codex"), str(workspace))
    backend = CodexBackend(store, FAKE_CODEX)
    try:
        await asyncio.wait_for(backend.start(rec, deny), 5)
        async with asyncio.timeout(8):
            frames = [f.to_wire() async for f in backend.run_turn(UserTurn("hi"))]
    finally:
        await backend.close()
        store.close()
    usage = _usage(frames)
    assert usage["scope"] == "conversation" and usage["cumulative"] is True
    assert usage["models"] == [
        {
            "model": "gpt-5.3-codex",
            "input": 100,
            "output": 20,
            "cached": 60,
            "cache_write": 5,
            "reasoning": 7,
        }
    ]
    assert usage["context_pct"] == pytest.approx(12.0)


def _lingcore_session(tmp_path: Path) -> tuple[Store, object]:
    workspace = tmp_path / "w"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\ntools: []\n"
    )
    store = Store(tmp_path / "gui.db")
    entry = ModelEntry("f", "Fake", "lingcore", options={"profile": str(profile)})
    return store, store.create(entry, str(workspace))


async def test_lingcore_reports_each_request_without_cumulating(tmp_path) -> None:
    class ReportingLLM:
        """Stands in for a metered LLMClient: streams, then reports usage."""

        def __init__(self, meter) -> None:
            self.meter = meter

        async def stream(self, messages, tools=None):
            yield LLMChunk(text_delta="hello")
            yield LLMChunk(tool_calls=None, finish_reason="stop")
            self.meter.record(TokenUsage("deepseek-v4", 40, 9, 12, 3))

    store, session = _lingcore_session(tmp_path)
    llm = ReportingLLM(None)
    backend = LingCoreBackend(store, ProfileCache(), lambda: llm)
    try:
        await backend.start(session, deny)
        # start() built the agent; its clients report into that agent's meter.
        assert backend.agent is not None
        llm.meter = backend.agent.usage_meter
        frames = [f.to_wire() async for f in backend.run_turn(UserTurn("hi"))]
    finally:
        await backend.close()
        store.close()
    usage = _usage(frames)
    assert usage["scope"] == "request" and usage["cumulative"] is False
    assert usage["models"] == [
        {
            "model": "deepseek-v4",
            "input": 40,
            "output": 9,
            "cached": 12,
            "cache_write": 0,
            "reasoning": 3,
        }
    ]
    assert usage["cost_usd"] is None
    # Usage precedes the turn's terminal frame, so a biller sees it in order.
    kinds = [f["type"] for f in frames]
    assert kinds.index("usage") < kinds.index("final")
