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
from agentgui.store import Store

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


@pytest.mark.parametrize("autonomy", ["read-only", "ask", "auto-edit"])
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
