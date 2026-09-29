from __future__ import annotations

import asyncio
from pathlib import Path

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
