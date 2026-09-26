"""Tiny line JSON-RPC app-server fixture used by adapter tests."""

from __future__ import annotations

import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if os.environ.get("FAKE_CODEX_LOG"):
        with open(os.environ["FAKE_CODEX_LOG"], "a") as out:
            out.write(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"userAgent": "fake"}
    elif method in {"thread/start", "thread/resume", "thread/fork"}:
        result = {"thread": {"id": "fake-thread"}, "model": "gpt-5.3-codex"}
    elif method == "turn/start":
        result = {"turn": {"id": "fake-turn"}}
    else:
        result = {}
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
    if method == "turn/start":
        print(
            json.dumps(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "fake-thread",
                        "turnId": "fake-turn",
                        "itemId": "item",
                        "delta": "hello",
                    },
                }
            ),
            flush=True,
        )
        usage = {
            "inputTokens": 100,
            "cachedInputTokens": 60,
            "cacheWriteInputTokens": 5,
            "outputTokens": 20,
            "reasoningOutputTokens": 7,
            "totalTokens": 120,
        }
        print(
            json.dumps(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "fake-thread",
                        "turnId": "fake-turn",
                        "tokenUsage": {
                            "total": usage,
                            "last": usage,
                            "modelContextWindow": 1000,
                        },
                    },
                }
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "fake-thread",
                        "turn": {"id": "fake-turn", "status": "completed"},
                    },
                }
            ),
            flush=True,
        )
