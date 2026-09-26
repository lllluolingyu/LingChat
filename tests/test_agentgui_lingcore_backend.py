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
