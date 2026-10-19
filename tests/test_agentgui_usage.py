"""Every backend reports usage in one frame shape a biller can diff."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from lingcore.llm import LLMChunk
from lingcore.message import ToolCall
from lingcore.usage import TokenUsage

import agentgui.usage
from agentgui.backends.base import UserTurn
from agentgui.backends.claude_backend import ClaudeBackend
from agentgui.backends.codex_backend import CodexBackend
from agentgui.backends.lingcore_backend import LingCoreBackend, ProfileCache
from agentgui.catalog import Catalog, ModelEntry
from agentgui.server import create_app
from agentgui.store import Store
from agentgui.usage import TurnClock
from tests.agentgui_fakes.socket import Socket

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
    # Flat chip values stay the turn's own main-loop counters, and cache
    # reads/writes are prompt tokens beside input rather than part of it.
    assert (usage["input"], usage["output"], usage["cached"]) == (12, 3, 4)
    assert usage["cache_in_input"] is False
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
    assert usage["cache_in_input"] is True
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
    assert usage["cache_in_input"] is True
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


def test_turn_clock_leaves_tool_and_approval_waits_out_of_model_time(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(agentgui.usage.time, "monotonic", lambda: now[0])
    clock = TurnClock()
    now[0] = 102.0  # 2s generating
    clock.observe({"type": "tool_call", "id": "a"})
    clock.observe({"type": "tool_call", "id": "b"})  # parallel: one span
    now[0] = 105.0
    clock.observe({"type": "tool_result", "id": "a"})
    clock.hold()  # an approval while "b" still runs adds no second span
    now[0] = 106.0
    clock.release()
    clock.observe({"type": "tool_result", "id": "b"})
    clock.observe({"type": "tool_result", "id": "unknown"})
    now[0] = 107.0  # 1s generating
    clock.hold()  # an approval with no tool running
    now[0] = 110.0
    clock.release()
    now[0] = 110.5  # 0.5s generating
    clock.observe({"type": "tool_call", "id": "c"})  # never resolves
    now[0] = 112.0
    timing = clock.finish()
    assert timing["type"] == "timing"
    assert (timing["elapsed_ms"], timing["model_ms"]) == (12000, 3500)
    assert timing["started_at"] <= timing["ended_at"]


async def test_lingcore_usage_and_timing_come_from_its_turn_stats(
    tmp_path,
) -> None:
    class ReportingLLM:
        """One tool round-trip, then an answer; each request is metered."""

        def __init__(self) -> None:
            self.meter = None
            self.calls = 0

        async def stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield LLMChunk(
                    tool_calls=[ToolCall(id="t1", name="list_dir", arguments={})],
                    finish_reason="tool_calls",
                )
                self.meter.record(TokenUsage("m", 100, 5, 80, 0))
                return
            yield LLMChunk(text_delta="done")
            yield LLMChunk(tool_calls=None, finish_reason="stop")
            self.meter.record(TokenUsage("m", 120, 7, 100, 0))

    workspace = tmp_path / "w"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\ntools: []\n"
    )
    catalog = Catalog(
        [ModelEntry("f", "Fake", "lingcore", options={"profile": str(profile)})]
    )
    llm = ReportingLLM()
    backends: list[LingCoreBackend] = []

    def factory(rec, store, profiles):
        backends.append(LingCoreBackend(store, profiles, lambda: llm))
        return backends[-1]

    app = create_app(
        catalog=catalog,
        store_path=tmp_path / "gui.db",
        auth_token="secret",
        backend_factory=factory,
    )
    rec = app.state.store.create(catalog.entries["f"], str(workspace))
    try:
        async with Socket(app, rec.id) as ws:
            assert (await ws.recv())["type"] == "hello"
            llm.meter = backends[-1].agent.usage_meter
            await ws.send({"type": "user", "text": "look around"})
            live = await ws.until("turn_end")
        kinds = [f["type"] for f in live]
        assert kinds.count("usage") == 2
        # LingCore measures its own turn; the connection's clock steps aside.
        assert kinds.count("timing") == 1
        assert kinds.index("timing") == kinds.index("final") - 1
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-AgentGUI-Token": "secret"},
        ) as client:
            turns = (await client.get(f"/api/sessions/{rec.id}")).json()["turns"]
    finally:
        app.state.store.close()
    # The rebuild takes usage and timing from LingCore's own turn_stats event,
    # anchored to the user row, instead of the copies the turn streamed.
    assert [f["type"] for f in turns[0]["frames"]] == [
        "user",
        "usage",
        "usage",
        "timing",
    ]
    frames = [f for t in turns for f in t["frames"]]
    stored = [f for f in frames if f["type"] == "usage"]
    assert [(f["input"], f["output"]) for f in stored] == [(100, 5), (120, 7)]
    (timing,) = [f for f in frames if f["type"] == "timing"]
    assert 0 <= timing["model_ms"] <= timing["elapsed_ms"]
    assert [t["role"] for t in turns] == ["user", "assistant", "tool", "assistant"]
