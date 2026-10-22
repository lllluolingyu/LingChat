from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lingcore.errors import LLMStreamError
from lingcore.guardrails import NoopGuardrail
from lingcore.llm import LLMChunk

from agentgui.backends.base import UserTurn
from agentgui.backends.lingcore_backend import LingCoreBackend, ProfileCache
from agentgui.catalog import ModelEntry
from agentgui.store import Store


class FakeLLM:
    async def stream(self, messages, tools=None):
        yield LLMChunk(text_delta="hello")
        yield LLMChunk(tool_calls=None, finish_reason="stop")


def test_lingcore_stream_is_unified(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\ntools: []\n"
    )

    async def run():
        store = Store(tmp_path / "gui.db")
        entry = ModelEntry(
            "fake", "Fake", "lingcore", options={"profile": str(profile)}
        )
        session = store.create(entry, str(workspace))
        backend = LingCoreBackend(store, ProfileCache(), FakeLLM)
        await backend.start(session, lambda request: asyncio.sleep(0, result="deny"))
        frames = [f.to_wire() async for f in backend.run_turn(UserTurn("hi"))]
        await backend.close()
        store.close()
        return frames

    frames = asyncio.run(run())
    assert frames[0] == {"type": "text", "delta": "hello"}
    assert frames[-1]["type"] == "final"


def test_ask_mode_keeps_only_tools_that_read_or_ask(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\n"
        "tools: [read_file, write_file, fetch_url, todo_write, calendar_add,"
        " calendar_list, calendar_remove]\n"
    )

    async def run(autonomy):
        store = Store(tmp_path / f"{autonomy}.db")
        entry = ModelEntry(
            "fake", "Fake", "lingcore", options={"profile": str(profile)}
        )
        session = store.create(entry, str(workspace), autonomy=autonomy)
        backend = LingCoreBackend(store, ProfileCache(), FakeLLM)
        await backend.start(session, lambda request: asyncio.sleep(0, result="deny"))
        tools = list(backend.profile.tools)
        await backend.close()
        store.close()
        return tools

    assert asyncio.run(run("ask")) == [
        "read_file",
        "fetch_url",
        "todo_write",
        "calendar_list",
    ]
    assert "calendar_remove" in asyncio.run(run("edit"))


def test_lingcore_todos_become_frames_and_survive_history_rebuild(tmp_path: Path):
    from lingcore.message import ToolCall

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\n"
        "tools: [todo_write]\n"
    )

    class TodoLLM:
        calls = 0

        async def stream(self, messages, tools=None):
            TodoLLM.calls += 1
            if TodoLLM.calls == 1:
                call = ToolCall(
                    id="t1",
                    name="todo_write",
                    arguments={"todos": [{"content": "ship", "status": "pending"}]},
                )
                yield LLMChunk(tool_calls=[call], finish_reason="tool_calls")
                return
            yield LLMChunk(text_delta="ok")
            yield LLMChunk(tool_calls=None, finish_reason="stop")

    async def run():
        store = Store(tmp_path / "gui.db")
        entry = ModelEntry(
            "fake", "Fake", "lingcore", options={"profile": str(profile)}
        )
        session = store.create(entry, str(workspace))
        backend = LingCoreBackend(store, ProfileCache(), TodoLLM)
        await backend.start(session, lambda request: asyncio.sleep(0, result="deny"))
        frames = [f.to_wire() async for f in backend.run_turn(UserTurn("go"))]
        backend.sync_history()
        turns = store.turns(session.id)
        await backend.close()
        store.close()
        return frames, turns

    frames, turns = asyncio.run(run())
    todos = [{"content": "ship", "status": "pending"}]
    assert {"type": "todos", "todos": todos} in frames
    rebuilt = [f for t in turns for f in t["frames"] if f["type"] == "todos"]
    assert rebuilt == [{"type": "todos", "todos": todos}]
    assert not any(
        f["type"] == "notice" and "todo_state" in f.get("text", "")
        for t in turns
        for f in t["frames"]
    )


@pytest.mark.parametrize("guarded", ["Partial answer", ""])
async def test_incomplete_response_rebuild_preserves_one_error(tmp_path, guarded):
    class Redactor(NoopGuardrail):
        async def post_output(self, text):
            assert text == "SECRET"
            return guarded

    class CutOffLLM:
        async def stream(self, messages, tools=None):
            yield LLMChunk(text_delta="SECRET")
            raise LLMStreamError(
                "reduce the thinking budget", retryable=False, finish_reason="length"
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\ntools: []\n"
    )
    store = Store(tmp_path / "gui.db")
    cache = ProfileCache()
    session = store.create(
        ModelEntry("fake", "Fake", "lingcore", options={"profile": str(profile)}),
        str(workspace),
    )
    backend = LingCoreBackend(store, cache, CutOffLLM)
    try:
        await backend.start(session, lambda request: asyncio.sleep(0, result="deny"))
        backend.agent.guardrail = Redactor()
        frames = [f.to_wire() async for f in backend.run_turn(UserTurn("hi"))]
        errors = [f for f in frames if f["type"] == "error"]
        assert len(errors) == 1 and errors[0]["finish_reason"] == "length"
        assert errors[0]["partial_content"] == guarded
        assert "reduce the thinking budget" in errors[0]["message"]
        assert not any(f["type"] == "final" for f in frames)
        backend.reconcile(errors)
        backend.sync_history()
        replay = [f for t in store.turns(session.id) for f in t["frames"]]
        assert [f for f in replay if f["type"] == "error"] == errors
        assert [f["delta"] for f in replay if f["type"] == "text"] == (
            [guarded] if guarded else []
        )
    finally:
        await backend.close()
        cache.close()
        store.close()
