"""Codex app-server adapter, matched to the checked-in 0.156.1 schema."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from agentgui.protocol import ApprovalRequest, Frame, frame, tool_kind
from agentgui.store import SessionRecord, Store
from agentgui.usage import model_usage, usage_frame

from ._jsonrpc import JsonRpc
from ._procs import resolve_executable
from .base import ApproveFn, BackendBase, Capabilities, UserTurn


class CodexBackend(BackendBase):
    capabilities = Capabilities(fork=True, images=True, thinking=True)

    def __init__(self, store: Store, command: list[str] | None = None) -> None:
        super().__init__(store)
        self.command = command
        self.rpc: JsonRpc | None = None
        self.turn_id: str | None = None
        self.task: asyncio.Task[Any] | None = None
        self.started: set[str] = set()
        self.items: dict[str, dict[str, Any]] = {}
        self.stopping = False
        self.model = ""  # replaced by the model thread/start reports

    def thread_options(self) -> dict[str, Any]:
        return {
            "cwd": self.session.workspace,
            "model": self.session.native_model or None,
            "approvalPolicy": "never"
            if self.session.autonomy == "auto-edit"
            else "on-request",
            "approvalsReviewer": "user",
            "sandbox": "read-only"
            if self.session.autonomy == "read-only"
            else "workspace-write",
        }

    async def start(self, session: SessionRecord, approve: ApproveFn) -> None:
        await super().start(session, approve)
        await self._connect()

    async def _connect(self) -> None:
        command = self.command or [
            resolve_executable("codex", self.session.workspace),
            "app-server",
            "--listen",
            "stdio://",
        ]
        self.rpc = JsonRpc(self._request)
        try:
            await self.rpc.start(command, self.session.workspace)
            await self.rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "agentgui", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.rpc.send({"method": "initialized", "params": {}})
            params = self.thread_options()
            method = "thread/start"
            if self.session.native_id:
                method = "thread/resume"
                params.update(threadId=self.session.native_id, excludeTurns=True)
            result = await self.rpc.request(method, params)
            # The served model can differ from the catalog's native name.
            served = result.get("model")
            if isinstance(served, str) and served:
                self.model = served
            self.save_native(result["thread"]["id"])
        except BaseException:
            await self.rpc.close()
            raise

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("threadId") != self.session.native_id or self.stopping:
            return {"decision": "decline"}
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            raise ValueError(f"unsupported server request: {method}")
        if (
            self.session.autonomy == "read-only"
            and method == "item/fileChange/requestApproval"
        ):
            return {"decision": "decline"}
        kind = "shell" if "commandExecution" in method else "edit"
        item = self.items.get(params.get("itemId", ""), {})
        diff = (
            "\n".join(change.get("diff", "") for change in item.get("changes", []))
            or None
        )
        detail = (
            params.get("command")
            or params.get("reason")
            or json.dumps(item or params, ensure_ascii=False)
        )
        answer = await self.approve(
            ApprovalRequest(
                kind,
                "Run shell command?" if kind == "shell" else "Apply file changes?",
                detail,
                diff=diff,
            )
        )
        return {
            "decision": {
                "once": "accept",
                "session": "acceptForSession",
                "deny": "decline",
            }[answer]
        }

    async def run_turn(self, inp: UserTurn) -> AsyncIterator[Frame]:
        assert self.rpc
        self.task = asyncio.current_task()
        self.stopping = False
        self.started.clear()
        self.items.clear()
        try:
            if self.rpc.proc and self.rpc.proc.returncode is not None:
                await self.rpc.close()
                await self._connect()
                yield frame(
                    "notice", level="warning", text="codex app-server restarted"
                )
            assert self.rpc
            inputs: list[dict[str, Any]] = []
            if inp.text:
                inputs.append({"type": "text", "text": inp.text, "text_elements": []})
            for a in inp.attachments:
                if a.kind != "image":
                    raise ValueError("Codex attachments currently support images only")
                inputs.append(
                    {"type": "image", "url": f"data:{a.media_type};base64,{a.data}"}
                )
            result = await self.rpc.request(
                "turn/start", {"threadId": self.session.native_id, "input": inputs}
            )
            self.turn_id = result["turn"]["id"]
            saw_text: set[str] = set()
            while True:
                msg = await self.rpc.notifications.get()
                if isinstance(msg, Exception):
                    raise msg
                method, p = msg["method"], msg.get("params", {})
                if p.get("threadId", self.session.native_id) != self.session.native_id:
                    continue
                if p.get("turnId", self.turn_id) != self.turn_id:
                    continue
                if method == "item/agentMessage/delta":
                    saw_text.add(p["itemId"])
                    yield frame("text", delta=p["delta"])
                elif method in {
                    "item/reasoning/summaryTextDelta",
                    "item/reasoning/textDelta",
                }:
                    yield frame("thinking", delta=p["delta"])
                elif method in {"item/started", "item/completed"}:
                    item = p["item"]
                    ident, kind = item["id"], item["type"]
                    self.items[ident] = item
                    if (
                        kind == "agentMessage"
                        and method == "item/completed"
                        and ident not in saw_text
                    ):
                        yield frame("text", delta=item.get("text", ""))
                    if kind not in {
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                        "webSearch",
                    }:
                        continue
                    name = item.get("tool", kind)
                    if ident not in self.started:
                        self.started.add(ident)
                        arguments = item.get("arguments") or {
                            "command": item.get("command", ""),
                            "changes": item.get("changes", []),
                        }
                        yield frame(
                            "tool_call",
                            id=ident,
                            name=name,
                            kind=tool_kind(kind),
                            arguments=arguments,
                        )
                    if method == "item/completed":
                        diff = (
                            "\n".join(
                                c.get("diff", "") for c in item.get("changes", [])
                            )
                            or None
                        )
                        ok = (
                            item.get("status") not in {"failed", "declined"}
                            and item.get("exitCode") in {None, 0}
                            and not item.get("error")
                        )
                        content = (
                            item.get("aggregatedOutput")
                            or item.get("result")
                            or item.get("error")
                            or item.get("status", "completed")
                        )
                        yield frame(
                            "tool_result",
                            id=ident,
                            name=name,
                            ok=ok,
                            content=content
                            if isinstance(content, str)
                            else json.dumps(content, ensure_ascii=False),
                            diff=diff,
                            attachments=[],
                        )
                elif method == "thread/tokenUsage/updated":
                    usage = p["tokenUsage"]
                    total = usage["total"]
                    window = usage.get("modelContextWindow")
                    # Codex reports thread running totals, and repeats an
                    # unchanged total on some notifications, so only the total
                    # is safe to diff for billing. cachedInputTokens and
                    # reasoningOutputTokens are parts of input/output.
                    yield usage_frame(
                        [
                            model_usage(
                                self.model,
                                input=total["inputTokens"],
                                output=total["outputTokens"],
                                cached=total["cachedInputTokens"],
                                cache_write=total.get("cacheWriteInputTokens", 0),
                                reasoning=total["reasoningOutputTokens"],
                            )
                        ],
                        scope="conversation",
                        cumulative=True,
                        input=total["inputTokens"],
                        output=total["outputTokens"],
                        cached=total["cachedInputTokens"],
                        context_pct=(
                            usage.get("last", total)["totalTokens"] / window * 100
                        )
                        if window
                        else None,
                    )
                elif method == "turn/completed":
                    turn = p["turn"]
                    if turn["id"] != self.turn_id:
                        continue
                    if turn["status"] == "interrupted" or self.stopping:
                        yield frame("cancelled")
                    elif turn["status"] == "failed":
                        error = turn.get("error") or {}
                        yield frame(
                            "error", message=error.get("message", "Codex turn failed")
                        )
                    else:
                        yield frame("final", content="")
                    break
                elif method == "error":
                    error = p.get("error", {})
                    text = error.get("message", str(error))
                    if p.get("willRetry"):
                        yield frame("notice", level="warning", text=text)
                    else:
                        yield frame("error", message=text)
                elif method == "stderr":
                    yield frame("notice", level="info", text=p["text"])
        finally:
            self.turn_id = None
            self.task = None

    async def stop(self) -> list[Frame]:
        self.stopping = True
        task = self.task
        if not self.rpc or not task:
            return [frame("cancelled")]
        if not self.turn_id:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.rpc.close()
            return [frame("cancelled")]
        await self.rpc.request(
            "turn/interrupt",
            {"threadId": self.session.native_id, "turnId": self.turn_id},
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), 10)
            return []
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.rpc.close()
            return [
                frame("cancelled"),
                frame(
                    "notice",
                    level="warning",
                    text="Codex interrupt timed out; the app-server was stopped.",
                ),
            ]

    async def fork(self, through_seq: int | None) -> SessionRecord:
        self.require_tip(through_seq)
        assert self.rpc
        result = await self.rpc.request(
            "thread/fork",
            {
                **self.thread_options(),
                "threadId": self.session.native_id,
                "excludeTurns": True,
                "ephemeral": False,
            },
        )
        return self.store.clone(self.session, result["thread"]["id"])

    async def close(self) -> None:
        if self.task:
            await self.stop()
        if self.rpc:
            await self.rpc.close()
