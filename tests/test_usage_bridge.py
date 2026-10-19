"""LingChat replays the per-turn usage and timing LingCore keeps with a session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lingcore.agent
from lingcore.llm import LLMChunk
from lingcore.message import ToolCall
from lingcore.usage import TokenUsage, UsageMeter
from starlette.testclient import TestClient

from lingchat.server import create_app


class MeteredLLM:
    """Scripted turns; each request reports usage into the newest agent's meter."""

    def __init__(self, turns: list[dict[str, Any]], meters: list[UsageMeter]) -> None:
        self.turns, self.meters = list(turns), meters

    async def stream(self, messages, tools=None):
        turn = self.turns.pop(0)
        if turn.get("text"):
            yield LLMChunk(text_delta=turn["text"])
        yield LLMChunk(tool_calls=turn.get("tool_calls"), finish_reason="stop")
        self.meters[-1].record(TokenUsage("m", *turn["usage"]))


def _drain_until(ws, stop_type: str) -> list[dict]:
    msgs = []
    while True:
        msgs.append(ws.receive_json())
        if msgs[-1]["type"] == stop_type:
            return msgs


def _turn_stats(client: TestClient, sid: str) -> list[tuple[int, list]]:
    events = client.get(f"/api/sessions/{sid}").json()["events"]
    return [
        (e["message_seq"], [(u["input"], u["output"]) for u in e["usage"]])
        for e in events
        if e["type"] == "turn_stats"
    ]


def test_turn_stats_stream_live_and_replay_through_edit_and_fork(
    tmp_path: Path, monkeypatch
) -> None:
    # A fake client is not wired to the agent's meter; capture each one.
    meters: list[UsageMeter] = []

    def meter() -> UsageMeter:
        meters.append(UsageMeter())
        return meters[-1]

    monkeypatch.setattr(lingcore.agent, "UsageMeter", meter)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hi", encoding="utf-8")
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: test\nworkspace: {workspace}\nllm:\n  model: fake\n"
        "tools: ['read_file']\n",
        encoding="utf-8",
    )
    read = ToolCall(id="t1", name="read_file", arguments={"path": "a.txt"})
    llm = MeteredLLM(
        [
            {"tool_calls": [read], "usage": (100, 5, 80, 0)},
            {"text": "first answer", "usage": (120, 7, 100, 0)},
            {"text": "second answer", "usage": (140, 9, 120, 0)},
            {"text": "edited answer", "usage": (90, 4, 0, 0)},
        ],
        meters,
    )
    app = create_app(profile, require_auth=False, llm_factory=lambda: llm)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "first question"})
        live = _drain_until(ws, "turn_end")
        kinds = [m["type"] for m in live]
        assert kinds.count("usage") == 2
        # LingCore's totals arrive right before the outcome.
        assert kinds[-3:] == ["timing", "final", "turn_end"]
        timing = live[-3]
        assert 0 <= timing["model_ms"] <= timing["elapsed_ms"]
        assert {"scope": "request", "cache_in_input": True}.items() <= live[
            kinds.index("usage")
        ].items()
        ws.send_json({"type": "user", "text": "second question"})
        _drain_until(ws, "turn_end")

        # Each turn's stats are anchored to the user message that started it.
        assert _turn_stats(client, sid) == [
            (0, [(100, 5), (120, 7)]),
            (4, [(140, 9)]),
        ]
        replayed = next(
            e
            for e in client.get(f"/api/sessions/{sid}").json()["events"]
            if e["type"] == "turn_stats"
        )
        assert replayed["timing"]["type"] == "timing"

        forked = client.post(f"/api/sessions/{sid}/fork", json={"through_seq": 3})
        assert _turn_stats(client, forked.json()["id"]) == [(0, [(100, 5), (120, 7)])]

        # Edit replaces the edited turn's stats rather than keeping both.
        ws.send_json({"type": "edit", "seq": 4, "text": "edited question"})
        _drain_until(ws, "turn_end")
        assert _turn_stats(client, sid) == [
            (0, [(100, 5), (120, 7)]),
            (4, [(90, 4)]),
        ]
