# Adapted from LingChat tests; Copyright LingChat contributors; Apache-2.0.
from __future__ import annotations

import asyncio

from lingcore.llm import LLMChunk


class FakeLLM:
    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = []

    async def stream(self, messages, tools=None):
        self.messages.append(messages)
        turn = self.turns.pop(0) if self.turns else {}
        if turn.get("wait"):
            yield LLMChunk(text_delta="working")
            await asyncio.Event().wait()
        text = turn.get("text", "")
        for i in range(0, len(text), 4):
            yield LLMChunk(text_delta=text[i : i + 4])
        yield LLMChunk(tool_calls=turn.get("tool_calls"), finish_reason="stop")
