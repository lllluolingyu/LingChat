from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator
from lingcore.message import Attachment

from agentgui.backends.base import UserTurn
from agentgui.backends.codex_backend import CodexBackend
from agentgui.catalog import ModelEntry
from agentgui.diagnostics import CODEX_SCHEMA_VERSION
from agentgui.store import AUTONOMY_LEVELS, Store

FIXTURE = Path(__file__).parent / "agentgui_fakes/fake_codex_appserver.py"
SCHEMA = Path(__file__).parent / "fixtures/codex-schema"
PARAMS = {
    "thread/start": "v2/ThreadStartParams.json",
    "thread/resume": "v2/ThreadResumeParams.json",
    "thread/fork": "v2/ThreadForkParams.json",
    "turn/start": "v2/TurnStartParams.json",
}
RESPONSES = {
    "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalResponse.json",
    "item/fileChange/requestApproval": "FileChangeRequestApprovalResponse.json",
}


def validate(schema: str, value: object) -> None:
    Draft7Validator(json.loads((SCHEMA / schema).read_text())).validate(value)


async def deny(request):
    return "deny"


async def test_codex_jsonrpc_fixture_streams(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    session = store.create(ModelEntry("codex", "Codex", "codex"), str(workspace))
    backend = CodexBackend(store, [sys.executable, str(FIXTURE.resolve())])
    try:
        await backend.start(session, deny)
        frames = [f.to_wire() async for f in backend.run_turn(UserTurn("hello"))]
    finally:
        await backend.close()
        store.close()
    assert frames[0] == {"type": "text", "delta": "hello"}
    assert frames[-1]["type"] == "final"


def test_schema_fixture_matches_supported_version():
    assert (SCHEMA / "VERSION").read_text().strip() == CODEX_SCHEMA_VERSION


@pytest.mark.parametrize("autonomy", sorted(AUTONOMY_LEVELS))
async def test_codex_requests_match_checked_in_schema(tmp_path, monkeypatch, autonomy):
    log = tmp_path / "codex.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    entry = ModelEntry("codex", "Codex", "codex", "gpt-5")
    session = store.create(entry, str(workspace), autonomy)
    command = [sys.executable, str(FIXTURE.resolve())]
    image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Pb8AAAAASUVORK5CYII="
    backend = CodexBackend(store, command)
    try:
        await backend.start(session, deny)
        turn = UserTurn(
            "hi", [Attachment(kind="image", media_type="image/png", data=image)]
        )
        [f async for f in backend.run_turn(turn)]
        await backend.fork(None)
        await backend.close()
        backend = CodexBackend(store, command)
        await backend.start(session, deny)  # native id set -> thread/resume
        for method, schema in RESPONSES.items():
            for answer in ["once", "session", "deny"]:

                async def reply(request, answer=answer):
                    return answer

                backend.approve = reply
                result = await backend._request(
                    method, {"threadId": session.native_id, "itemId": "item"}
                )
                validate(schema, result)
    finally:
        await backend.close()
        store.close()
    sent = [json.loads(line) for line in log.read_text().splitlines()]
    methods = {m.get("method") for m in sent}
    assert set(PARAMS) <= methods
    for message in sent:
        if message.get("method") in PARAMS:
            validate(PARAMS[message["method"]], message["params"])
    # Pin the level to the sandbox it buys. Only the sandbox separates the two:
    # `edit` still asks to leave the workspace, so the policy never varies.
    expected = "read-only" if autonomy == "ask" else "workspace-write"
    for message in sent:
        if message.get("method") in PARAMS and "sandbox" in message["params"]:
            assert message["params"]["sandbox"] == expected, message["method"]
            assert message["params"]["approvalPolicy"] == "on-request"


async def test_ask_routes_a_file_change_to_the_user(tmp_path: Path):
    """`ask` runs read-only, so this request is how a write gets granted at all.

    It used to be auto-declined, which combined with the read-only sandbox left
    no way to approve a write without changing the session's level.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    session = store.create(ModelEntry("codex", "Codex", "codex"), str(workspace), "ask")
    backend = CodexBackend(store, [sys.executable, str(FIXTURE.resolve())])
    try:
        await backend.start(session, deny)
        asked = []

        async def approve(request):
            asked.append(request.kind)
            return "once"

        backend.approve = approve
        result = await backend._request(
            "item/fileChange/requestApproval",
            {"threadId": session.native_id, "itemId": "item"},
        )
    finally:
        await backend.close()
        store.close()
    assert asked == ["edit"]
    assert result == {"decision": "accept"}
