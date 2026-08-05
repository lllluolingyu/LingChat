"""Tests for LingChat's session REST endpoints and WebSocket resume flow.

Same approach as test_bridge.py: the real FastAPI app via Starlette's
TestClient with a scripted fake LLM. Profiles are written to tmp_path, which
sits outside the lingcore package tree, so sessions are enabled by default and
the store lands at ``<tmp profile dir>/sessions.db``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any, AsyncIterator

from lingcore.llm import LLMChunk
from lingcore.message import Message, ToolCall
from starlette.testclient import TestClient

from lingchat.server import create_app


class FakeLLM:
    """Scripted fake that also records the messages seen by each call."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.calls: list[list[Message]] = []
        self.tool_schemas: list[list[dict[str, Any]] | None] = []

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        self.tool_schemas.append(tools)
        if not self._turns:
            yield LLMChunk(tool_calls=None, finish_reason="stop")
            return
        turn = self._turns.pop(0)
        text = turn.get("text", "")
        for i in range(0, len(text), 4):
            yield LLMChunk(text_delta=text[i : i + 4])
        if turn.get("block"):
            await asyncio.Event().wait()
        yield LLMChunk(tool_calls=turn.get("tool_calls"), finish_reason="stop")


def _write_profile(
    tmp_path: Path,
    extra: str = "",
    *,
    tools: str = "['read_file']",
) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"name: test\nworkspace: {ws}\nllm:\n  model: fake\ntools: {tools}\n" + extra,
        encoding="utf-8",
    )
    return cfg


def _drain_until(ws, stop_type: str) -> list[dict]:
    msgs = []
    while True:
        m = ws.receive_json()
        msgs.append(m)
        if m["type"] == stop_type:
            return msgs


def _delete_when_released(client: TestClient, sid: str):
    """DELETE a session, allowing the just-closed socket's detach to land."""
    for _ in range(100):
        r = client.delete(f"/api/sessions/{sid}")
        if r.status_code != 409:
            return r
        time.sleep(0.01)
    return r


def test_turn_is_stored_and_listed(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(
        profile, require_auth=False, llm_factory=lambda: FakeLLM([{"text": "Hello!"}])
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        sid = hello["session"]
        assert sid is not None and len(sid) == 32
        assert hello["title"] == ""  # fresh session: no row yet

        ws.send_json({"type": "user", "text": "first question"})
        _drain_until(ws, "turn_end")

    listing = client.get("/api/sessions").json()
    assert listing["enabled"] is True
    assert [s["id"] for s in listing["sessions"]] == [sid]
    assert listing["sessions"][0]["title"] == "first question"
    assert listing["sessions"][0]["message_count"] == 2


def test_websocket_attachment_reaches_agent(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM([{"text": "seen"}])
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)
    payload = {
        "kind": "image",
        "media_type": "image/png",
        "name": "pic.png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\nrest").decode("ascii"),
    }

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "describe", "attachments": [payload]})
        _drain_until(ws, "turn_end")

    assert fake.calls[0][1].role == "user"
    assert fake.calls[0][1].attachments[0].name == "pic.png"
    data = client.get(f"/api/sessions/{sid}").json()
    assert data["messages"][0]["attachments"][0]["media_type"] == "image/png"
    assert data["messages"][0]["text"] == "describe"


def test_websocket_over_total_attachment_limit_is_per_turn_error(tmp_path, monkeypatch):
    # Per-attachment checks pass but the aggregate exceeds the UserInput total
    # cap. That must come back as an error message on the socket — and the
    # connection must survive to run a normal turn afterwards.
    import lingcore.message as message_mod

    monkeypatch.setattr(message_mod, "TOTAL_ATTACHMENT_MAX_BYTES", 32)
    profile = _write_profile(tmp_path)
    fake = FakeLLM([{"text": "still alive"}])
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)
    payload = {
        "kind": "image",
        "media_type": "image/png",
        "name": "pic.png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 24).decode("ascii"),
    }

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user", "text": "big", "attachments": [payload, payload]})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "attachment" in err["message"]
        assert ws.receive_json()["type"] == "turn_end"

        # The socket is still usable.
        ws.send_json({"type": "user", "text": "hello"})
        msgs = _drain_until(ws, "turn_end")
        assert any(m["type"] == "final" and m["text"] == "still alive" for m in msgs)


def test_transcript_display_shapes(tmp_path):
    profile = _write_profile(tmp_path)
    turns = [
        {
            "tool_calls": [
                ToolCall(id="c1", name="read_file", arguments={"path": "missing.txt"})
            ]
        },
        {"text": "could not read it"},
    ]
    app = create_app(profile, require_auth=False, llm_factory=lambda: FakeLLM(turns))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "read that file"})
        _drain_until(ws, "turn_end")

    data = client.get(f"/api/sessions/{sid}").json()
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert data["messages"][0]["text"] == "read that file"
    assert data["messages"][1]["tool_calls"] == [
        {"id": "c1", "name": "read_file", "arguments": {"path": "missing.txt"}}
    ]
    assert data["messages"][2]["id"] == "c1"  # pairs the result with its call
    assert data["messages"][2]["ok"] is False  # ERROR: prefix → failed result
    assert data["messages"][3]["text"] == "could not read it"


def test_resume_restores_history(tmp_path):
    profile = _write_profile(tmp_path)
    fake1 = FakeLLM([{"text": "first answer"}])
    fake2 = FakeLLM([{"text": "second answer"}])
    fakes = [fake1, fake2]
    app = create_app(profile, require_auth=False, llm_factory=lambda: fakes.pop(0))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "question one"})
        _drain_until(ws, "turn_end")

    with client.websocket_connect(f"/ws?session={sid}") as ws:
        hello = ws.receive_json()
        assert hello["session"] == sid
        assert hello["title"] == "question one"
        ws.send_json({"type": "user", "text": "question two"})
        msgs = _drain_until(ws, "turn_end")
        assert any(m["type"] == "final" and m["text"] == "second answer" for m in msgs)

    # The resumed agent's first LLM call saw the stored turn-1 history.
    contents = [m.content for m in fake2.calls[0]]
    assert "question one" in contents and "first answer" in contents

    data = client.get(f"/api/sessions/{sid}").json()
    assert [m["role"] for m in data["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_compaction_event_is_forwarded(tmp_path):
    profile = _write_profile(
        tmp_path,
        extra=(
            "memory:\n"
            "  max_messages: 50\n"
            "  max_tokens: 120\n"
            "  compaction:\n"
            "    enabled: true\n"
            "    compact_at_ratio: 0.1\n"
            "    keep_recent_ratio: 0.05\n"
            "    max_summary_chars: 1000\n"
        ),
    )
    fake = FakeLLM(
        [
            {"text": "first answer"},
            {"text": "compact summary"},
            {"text": "second answer"},
        ]
    )
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "first " + ("padding " * 40)})
        _drain_until(ws, "turn_end")

        ws.send_json({"type": "user", "text": "second question"})
        msgs = _drain_until(ws, "turn_end")

    compact = next(m for m in msgs if m["type"] == "compact")
    assert compact["summarized_messages"] >= 1
    assert compact["before_tokens"] > compact["after_tokens"]
    assert any(m["type"] == "final" and m["text"] == "second answer" for m in msgs)

    transcript = client.get(f"/api/sessions/{sid}").json()
    assert transcript["event_cursor"] >= 1
    persisted = next(
        event for event in transcript["events"] if event["type"] == "compact"
    )
    assert persisted["message_seq"] == 2
    assert persisted["summarized_messages"] == compact["summarized_messages"]
    assert persisted["before_tokens"] == compact["before_tokens"]
    assert persisted["after_tokens"] == compact["after_tokens"]

    replay = client.get(f"/api/sessions/{sid}/events?after=-1").json()
    assert replay["session"] == sid
    assert replay["events"] == transcript["events"]
    assert replay["cursor"] == transcript["event_cursor"]
    assert client.get(
        f"/api/sessions/{sid}/events?after={replay['cursor']}"
    ).json() == {"session": sid, "events": [], "cursor": replay["cursor"]}
    assert client.get(f"/api/sessions/{sid}/events?after=-2").status_code == 422
    assert client.get(f"/api/sessions/{'f' * 32}/events").status_code == 404


def test_dynamic_skill_state_persists_and_replays_after_reconnect(tmp_path):
    profile = _write_profile(
        tmp_path,
        extra="initial_tools: ['activate_skill']\n",
        tools="['activate_skill', 'read_file', 'search']",
    )
    activation = ToolCall(
        id="activate-review",
        name="activate_skill",
        arguments={"name": "code-review"},
    )
    first = FakeLLM(
        [
            {"tool_calls": [activation]},
            {"text": "review mode ready"},
        ]
    )
    resumed = FakeLLM([{"text": "still reviewing"}])
    fakes = [first, resumed]
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: fakes.pop(0),
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        sid = hello["session"]
        assert hello["event_cursor"] == -1
        ws.send_json({"type": "user", "text": "activate review mode"})
        frames = _drain_until(ws, "turn_end")
        assert any(
            frame == {"type": "skill", "name": "code-review", "active": True}
            for frame in frames
        )

    transcript = client.get(f"/api/sessions/{sid}").json()
    skill_event = next(
        event for event in transcript["events"] if event["type"] == "skill_state"
    )
    assert skill_event["active"] == ["code-review"]
    assert skill_event["activated"] == ["code-review"]
    assert skill_event["deactivated"] == []

    with client.websocket_connect(f"/ws?session={sid}") as ws:
        hello = ws.receive_json()
        assert hello["event_cursor"] == transcript["event_cursor"]
        ws.send_json({"type": "user", "text": "continue the review"})
        _drain_until(ws, "turn_end")

    assert "Group findings by severity" in resumed.calls[0][0].content
    names = {schema["function"]["name"] for schema in (resumed.tool_schemas[0] or [])}
    assert {"activate_skill", "read_file", "search"} <= names


def test_unknown_valid_id_is_adopted_and_malformed_replaced(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(
        profile, require_auth=False, llm_factory=lambda: FakeLLM([{"text": "hi"}])
    )
    client = TestClient(app)

    minted = "ab" * 16
    with client.websocket_connect(f"/ws?session={minted}") as ws:
        assert ws.receive_json()["session"] == minted
        ws.send_json({"type": "user", "text": "speak"})
        _drain_until(ws, "turn_end")
    assert client.get(f"/api/sessions/{minted}").status_code == 200

    with client.websocket_connect("/ws?session=not-a-valid-id") as ws:
        sid = ws.receive_json()["session"]
        assert sid != "not-a-valid-id" and len(sid) == 32


def test_concurrent_attach_refused(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(profile, require_auth=False, llm_factory=lambda: FakeLLM([]))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws1:
        sid = ws1.receive_json()["session"]
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            busy = ws2.receive_json()
            assert busy == {"type": "session_busy", "session": sid}


def test_delete_and_rename(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(
        profile, require_auth=False, llm_factory=lambda: FakeLLM([{"text": "yo"}])
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "hello"})
        _drain_until(ws, "turn_end")

        # Attached: deletion refused so the live agent can't resurrect the row.
        assert client.delete(f"/api/sessions/{sid}").status_code == 409

    r = client.patch(f"/api/sessions/{sid}", json={"title": "renamed chat"})
    assert r.status_code == 200 and r.json()["title"] == "renamed chat"
    assert (
        client.patch(f"/api/sessions/{sid}", json={"title": "   "}).status_code == 422
    )
    assert (
        client.patch(f"/api/sessions/{'9' * 32}", json={"title": "x"}).status_code
        == 404
    )

    assert _delete_when_released(client, sid).status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_sessions_disabled_profile(tmp_path):
    profile = _write_profile(tmp_path, extra="sessions:\n  enabled: false\n")
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: FakeLLM([{"text": "ephemeral"}]),
    )
    client = TestClient(app)

    listing = client.get("/api/sessions").json()
    assert listing == {"enabled": False, "notice": None, "sessions": []}

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["session"] is None
        ws.send_json({"type": "user", "text": "hi"})
        msgs = _drain_until(ws, "turn_end")
        assert any(m["type"] == "final" for m in msgs)

    assert not (tmp_path / "sessions.db").exists()
    assert client.get(f"/api/sessions/{'a' * 32}").status_code == 404


def test_sessions_in_package_profile_serves_notice(tmp_path, monkeypatch):
    """A profile inside the installed package can't persist — the API says why."""
    import lingcore.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_PACKAGE_DIR", tmp_path.resolve())
    profile = _write_profile(tmp_path)
    app = create_app(profile, require_auth=False, llm_factory=lambda: FakeLLM([]))
    client = TestClient(app)

    listing = client.get("/api/sessions").json()
    assert listing["enabled"] is False
    assert "inside the installed" in listing["notice"]
    assert not (tmp_path / "sessions.db").exists()


def test_stop_cancels_stream_repairs_session_and_allows_next_turn(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM(
        [
            {"text": "partial reply", "block": True},
            {"text": "recovered answer"},
        ]
    )
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "slow question"})
        assert ws.receive_json() == {"type": "text", "text": "part"}

        # A second submission is refused rather than queued behind the active turn.
        ws.send_json({"type": "user", "text": "do not queue this"})
        busy_frames = _drain_until(ws, "turn_busy")
        assert busy_frames[-1] == {"type": "turn_busy"}

        ws.send_json({"type": "stop"})
        stopped = _drain_until(ws, "turn_end")
        assert any(
            message["type"] == "cancelled" and message["reason"] == "stopped by user"
            for message in stopped
        )
        assert not any(message["type"] == "final" for message in stopped)

        ws.send_json({"type": "user", "text": "try again"})
        recovered = _drain_until(ws, "turn_end")
        assert any(
            message["type"] == "final" and message["text"] == "recovered answer"
            for message in recovered
        )

    data = client.get(f"/api/sessions/{sid}").json()
    assert [message["role"] for message in data["messages"]] == [
        "user",
        "user",
        "assistant",
    ]
    assert all(
        "partial reply" not in message.get("text", "") for message in data["messages"]
    )


def test_edit_rewinds_tail_and_regenerates_from_edited_user_message(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM(
        [
            {"text": "first answer"},
            {"text": "second answer"},
            {"text": "edited answer"},
        ]
    )
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "first question"})
        _drain_until(ws, "turn_end")
        ws.send_json({"type": "user", "text": "second question"})
        _drain_until(ws, "turn_end")

        before = client.get(f"/api/sessions/{sid}").json()
        assert [message["seq"] for message in before["messages"]] == [0, 1, 2, 3]

        ws.send_json({"type": "edit", "seq": 0, "text": "edited first question"})
        edited = _drain_until(ws, "turn_end")
        assert any(
            message
            == {
                "type": "edit_accepted",
                "seq": 0,
                "text": "edited first question",
            }
            for message in edited
        )
        assert any(
            message["type"] == "final" and message["text"] == "edited answer"
            for message in edited
        )

    after = client.get(f"/api/sessions/{sid}").json()
    assert [(message["seq"], message["role"]) for message in after["messages"]] == [
        (0, "user"),
        (1, "assistant"),
    ]
    assert after["messages"][0]["text"] == "edited first question"
    assert after["messages"][1]["text"] == "edited answer"
    assert after["title"] == "edited first question"
    regenerated_context = [message.content for message in fake.calls[2]]
    assert "edited first question" in regenerated_context
    assert "first answer" not in regenerated_context
    assert "second question" not in regenerated_context


def test_invalid_empty_edit_does_not_mutate_session(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: FakeLLM([{"text": "answer"}]),
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "question"})
        _drain_until(ws, "turn_end")
        ws.send_json({"type": "edit", "seq": 0, "text": "   "})
        rejected = ws.receive_json()
        assert rejected["type"] == "edit_rejected"
        assert "cannot be empty" in rejected["message"]

    data = client.get(f"/api/sessions/{sid}").json()
    assert [message["text"] for message in data["messages"]] == [
        "question",
        "answer",
    ]


def test_edit_preserves_original_attachments(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM(
        [
            {"text": "first answer"},
            {"text": "image answer"},
            {"text": "edited image answer"},
        ]
    )
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)
    payload = {
        "kind": "image",
        "media_type": "image/png",
        "name": "pic.png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\nrest").decode("ascii"),
    }

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "first question"})
        _drain_until(ws, "turn_end")
        ws.send_json(
            {"type": "user", "text": "describe this", "attachments": [payload]}
        )
        _drain_until(ws, "turn_end")

        ws.send_json({"type": "edit", "seq": 2, "text": "inspect this closely"})
        _drain_until(ws, "turn_end")

    regenerated_user = fake.calls[2][-1]
    assert regenerated_user.role == "user"
    assert regenerated_user.attachments[0].name == "pic.png"
    assert regenerated_user.content.count("[attached:") == 1
    data = client.get(f"/api/sessions/{sid}").json()
    assert data["messages"][2]["attachments"][0]["name"] == "pic.png"
    assert data["messages"][2]["text"] == "inspect this closely"


def test_fork_endpoint_preserves_source_and_supports_regeneration(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM(
        [
            {"text": "first answer"},
            {"text": "original second answer"},
            {"text": "forked second answer"},
        ]
    )
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    client = TestClient(app)
    attachment = {
        "kind": "image",
        "media_type": "image/png",
        "name": "branch.png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\nbranch").decode("ascii"),
    }

    with client.websocket_connect("/ws") as ws:
        source = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "first question"})
        _drain_until(ws, "turn_end")
        ws.send_json(
            {
                "type": "user",
                "text": "second question",
                "attachments": [attachment],
            }
        )
        _drain_until(ws, "turn_end")

        response = client.post(
            f"/api/sessions/{source}/fork",
            json={"through_seq": 2, "title": "alternate branch"},
        )
        assert response.status_code == 200
        forked = response.json()
        destination = forked["id"]
        assert destination != source
        assert forked["title"] == "alternate branch"
        assert forked["message_count"] == 3
        assert forked["fork"] == {
            "parent_session_id": source,
            "root_session_id": source,
            "through_seq": 2,
        }

        assert (
            client.post(
                f"/api/sessions/{source}/fork", json={"through_seq": 99}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/sessions/{source}/fork", json={"through_seq": -1}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/sessions/{source}/fork", json={"through_seq": True}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/sessions/{source}/fork", json={"title": "   "}
            ).status_code
            == 422
        )
        assert client.post(f"/api/sessions/{'e' * 32}/fork", json={}).status_code == 404

    copied = client.get(f"/api/sessions/{destination}").json()
    assert [message["text"] for message in copied["messages"]] == [
        "first question",
        "first answer",
        "second question",
    ]
    assert copied["messages"][2]["attachments"][0]["name"] == "branch.png"

    with client.websocket_connect(f"/ws?session={destination}") as ws:
        assert ws.receive_json()["session"] == destination
        ws.send_json({"type": "edit", "seq": 2, "text": "second question"})
        frames = _drain_until(ws, "turn_end")
        assert any(frame["type"] == "edit_accepted" for frame in frames)
        assert any(
            frame["type"] == "final" and frame["text"] == "forked second answer"
            for frame in frames
        )

    source_data = client.get(f"/api/sessions/{source}").json()
    assert [message["text"] for message in source_data["messages"]] == [
        "first question",
        "first answer",
        "second question",
        "original second answer",
    ]
    destination_data = client.get(f"/api/sessions/{destination}").json()
    assert [message["text"] for message in destination_data["messages"]] == [
        "first question",
        "first answer",
        "second question",
        "forked second answer",
    ]
    regenerated_context = [message.content for message in fake.calls[2]]
    assert "first answer" in regenerated_context
    assert any("second question" in content for content in regenerated_context)
    assert all(
        "original second answer" not in content for content in regenerated_context
    )
    assert fake.calls[2][-1].attachments[0].name == "branch.png"
