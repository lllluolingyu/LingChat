"""Claude Code via its SDK and the user's installed, authenticated CLI."""

from __future__ import annotations

import asyncio
import difflib
import json
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from agentgui.protocol import ApprovalRequest, Frame, frame, tool_kind
from agentgui.store import Autonomy, SessionRecord, Store
from agentgui.usage import model_usage, usage_frame

from ._procs import kill_group, resolve_executable
from .base import ApproveFn, BackendBase, Capabilities, UserTurn

# ``default`` prompts through ``can_use_tool`` for every write and command, so
# ``ask`` can approve one inline; ``acceptEdits`` auto-accepts file edits and
# still prompts for shell. Keys must match ``store.AUTONOMY_LEVELS``.
_PERMISSION_MODES: dict[Autonomy, Literal["default", "acceptEdits"]] = {
    "ask": "default",
    "edit": "acceptEdits",
}


class GroupTransport(SubprocessCLITransport):
    """SDK 0.2.159 transport with process-group ownership (covered by fake CLI)."""

    def _build_command(self) -> list[str]:
        command = super()._build_command()
        return [
            sys.executable,
            str(Path(__file__).with_name("_claude_launch.py")),
            *command,
        ]

    async def close(self) -> None:
        pid = self._process.pid if self._process else None
        try:
            await super().close()
        finally:
            if pid:
                kill_group(pid, signal.SIGKILL)


def edit_diff(name: str, inputs: dict[str, Any]) -> str | None:
    if name not in {"Edit", "Write", "MultiEdit"}:
        return None
    path = str(inputs.get("file_path", "file"))
    edits = inputs.get("edits", [inputs])
    return "".join(
        "".join(
            difflib.unified_diff(
                str(edit.get("old_string", "")).splitlines(keepends=True),
                str(edit.get("new_string", edit.get("content", ""))).splitlines(
                    keepends=True
                ),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        for edit in edits
    )


class ClaudeBackend(BackendBase):
    capabilities = Capabilities(fork=True, images=True, thinking=True, cost=True)

    def __init__(self, store: Store, executable: str | None = None) -> None:
        super().__init__(store)
        self.executable = executable
        self.client: ClaudeSDKClient | None = None
        self.task: asyncio.Task[Any] | None = None
        self.allowed: set[tuple[str, str]] = set()
        self.notices: asyncio.Queue[str] = asyncio.Queue()
        self.stopping = False

    async def start(self, session: SessionRecord, approve: ApproveFn) -> None:
        await super().start(session, approve)
        exe = self.executable or resolve_executable("claude", session.workspace)
        options = ClaudeAgentOptions(
            cli_path=exe,
            cwd=session.workspace,
            model=session.native_model or None,
            permission_mode=_PERMISSION_MODES[session.autonomy],
            can_use_tool=self.can_use_tool,
            include_partial_messages=True,
            resume=session.native_id,
            fork_session=bool(session.options.get("_fork_pending")),
            setting_sources=["user", "project", "local"],
            stderr=self.notices.put_nowait,
        )

        async def empty() -> AsyncIterator[dict[str, Any]]:
            if False:
                yield {}

        transport = GroupTransport(prompt=empty(), options=options)
        self.client = ClaudeSDKClient(options=options, transport=transport)
        await self.client.connect()

    async def can_use_tool(
        self, name: str, inputs: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        kind = tool_kind(name)
        # Both levels reach the approval path: under ``ask`` a write is something
        # the user may grant, not something refused on their behalf. Reads never
        # arrive here — ``default`` mode allows them without consulting us.
        # Exact command / path scope is conservative: compound commands never
        # inherit authorization from a shared first word such as `python`.
        scope = str(
            inputs.get(
                "command", inputs.get("file_path", json.dumps(inputs, sort_keys=True))
            )
        )
        rule = (name, scope)
        if rule in self.allowed:
            return PermissionResultAllow(updated_input=inputs)
        answer = await self.approve(
            ApprovalRequest(
                kind,
                context.title or f"Allow {name}?",
                scope,
                diff=edit_diff(name, inputs),
            )
        )
        if answer == "deny":
            return PermissionResultDeny(message="Denied by user")
        if answer == "session":
            self.allowed.add(rule)
        return PermissionResultAllow(updated_input=inputs)

    def confirm_native(self, native_id: str) -> None:
        # Once Claude reports the session id, a pending fork has materialized.
        self.session.options.pop("_fork_pending", None)
        self.save_native(native_id)

    async def run_turn(self, inp: UserTurn) -> AsyncIterator[Frame]:
        assert self.client
        self.task = asyncio.current_task()
        self.stopping = False
        streamed_text = False
        streamed_thinking = False

        async def prompt() -> AsyncIterator[dict[str, Any]]:
            content: list[dict[str, Any]] = []
            if inp.text:
                content.append({"type": "text", "text": inp.text})
            for attachment in inp.attachments:
                if attachment.kind != "image":
                    raise ValueError("Claude attachments currently support images only")
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": attachment.media_type,
                            "data": attachment.data,
                        },
                    }
                )
            yield {"type": "user", "message": {"role": "user", "content": content}}

        try:
            await self.client.query(prompt())
            async for message in self.client.receive_response():
                while not self.notices.empty():
                    yield frame("notice", level="info", text=self.notices.get_nowait())
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    ident = message.data.get("session_id")
                    if ident:
                        self.confirm_native(ident)
                elif (
                    isinstance(message, StreamEvent) and not message.parent_tool_use_id
                ):
                    event = message.event
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        streamed_text = True
                        yield frame("text", delta=delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        streamed_thinking = True
                        yield frame("thinking", delta=delta.get("thinking", ""))
                elif isinstance(message, (AssistantMessage, UserMessage)):
                    if isinstance(message.content, str):
                        continue
                    for block in message.content:
                        if (
                            isinstance(block, TextBlock)
                            and isinstance(message, AssistantMessage)
                            and not streamed_text
                        ):
                            yield frame("text", delta=block.text)
                        elif isinstance(block, ThinkingBlock) and not streamed_thinking:
                            yield frame("thinking", delta=block.thinking)
                        elif isinstance(block, ToolUseBlock):
                            yield frame(
                                "tool_call",
                                id=block.id,
                                name=block.name,
                                kind=tool_kind(block.name),
                                arguments=block.input,
                            )
                        elif isinstance(block, ToolResultBlock):
                            content = (
                                block.content
                                if isinstance(block.content, str)
                                else json.dumps(block.content, ensure_ascii=False)
                            )
                            yield frame(
                                "tool_result",
                                id=block.tool_use_id,
                                ok=not block.is_error,
                                content=content,
                                attachments=[],
                            )
                    if isinstance(message, AssistantMessage):
                        streamed_text = streamed_thinking = False
                elif isinstance(message, ResultMessage):
                    self.confirm_native(message.session_id)
                    usage = message.usage or {}
                    # usage covers this turn's main loop only; model_usage is
                    # the session running total and includes subagents, so it
                    # is what a biller diffs. total_cost_usd is likewise
                    # cumulative (and only the CLI's own estimate).
                    yield usage_frame(
                        [
                            model_usage(
                                name,
                                input=per.get("inputTokens", 0),
                                output=per.get("outputTokens", 0),
                                cached=per.get("cacheReadInputTokens", 0),
                                cache_write=per.get("cacheCreationInputTokens", 0),
                            )
                            for name, per in (message.model_usage or {}).items()
                        ],
                        scope="turn",
                        cumulative=True,
                        input=usage.get("input_tokens", 0),
                        output=usage.get("output_tokens", 0),
                        cached=usage.get("cache_read_input_tokens", 0),
                        cost_usd=message.total_cost_usd,
                    )
                    if self.stopping:
                        yield frame("cancelled")
                    elif message.is_error:
                        yield frame(
                            "error",
                            message=message.result
                            or "; ".join(message.errors or [])
                            or message.subtype,
                        )
                    else:
                        yield frame("final", content=message.result or "")
        finally:
            self.task = None

    async def stop(self) -> list[Frame]:
        if not self.client or not self.task:
            return [frame("cancelled")]
        self.stopping = True
        await self.client.interrupt()
        # The run_turn consumer continues draining through ResultMessage.
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 10)
            return []
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            await self.client.disconnect()
            return [
                frame("cancelled"),
                frame(
                    "error",
                    message="Claude did not finish interrupting; reconnect this chat.",
                ),
            ]

    async def fork(self, through_seq: int | None) -> SessionRecord:
        self.require_tip(through_seq)
        options = {**self.session.options, "_fork_pending": True}
        # Claude materializes the new native id on its first query. The marker
        # survives reloads until init/result confirms that new id.
        return self.store.clone(self.session, self.session.native_id, options=options)

    async def close(self) -> None:
        if self.task:
            await self.stop()
        if self.client:
            await self.client.disconnect()
