"""Claude Code via its SDK and the user's installed, authenticated CLI."""

from __future__ import annotations

import asyncio
import difflib
import json
import signal
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import replace
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
        self.reader: asyncio.Task[None] | None = None
        self.outgoing: asyncio.Queue[Frame | None] = asyncio.Queue(maxsize=256)
        self.pending = False
        self.responding = False
        self.closed = False
        self.failure: str | None = None
        self.settled = asyncio.Event()
        self.settled.set()
        self.displayed_tools: dict[str, asyncio.Event] = {}
        self.allowed: set[tuple[str, str]] = set()
        self.notices: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
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
            stderr=self.stderr,
            # Echoes identify injected monitor input separately from human input.
            extra_args={"replay-user-messages": None},
        )

        async def empty() -> AsyncIterator[dict[str, Any]]:
            if False:
                yield {}

        # The SDK adds ``--permission-prompt-tool stdio`` only to the options it
        # builds *its own* transport from; a custom transport gets them raw, and
        # without the flag the CLI never asks ``can_use_tool`` -- it denies every
        # prompt-worthy call outright ("you haven't granted it yet"). Setting it
        # on the client's options too would trip the SDK's mutual-exclusion check.
        transport = GroupTransport(
            prompt=empty(),
            options=replace(options, permission_prompt_tool_name="stdio"),
        )
        self.client = ClaudeSDKClient(options=options, transport=transport)
        await self.client.connect()
        self.reader = asyncio.create_task(self._receive())

    def stderr(self, text: str) -> None:
        if self.notices.full():
            self.notices.get_nowait()
        self.notices.put_nowait(text[:8000])

    @property
    def busy(self) -> bool:
        return self.pending or self.responding or not self.outgoing.empty()

    async def events(self) -> AsyncGenerator[Frame, None]:
        """The sole output consumer, kept alive across native result messages."""
        while True:
            value = await self.outgoing.get()
            try:
                if value is None:
                    return
                yield value
                if value.type == "tool_call":
                    event = self.displayed_tools.get(value.data["id"])
                    if event:
                        event.set()
            finally:
                self.outgoing.task_done()

    async def _emit(self, value: Frame) -> None:
        await self.outgoing.put(value)

    async def _begin_response(self, origin: dict[str, Any] | None = None) -> None:
        if not self.responding:
            self.responding = True
            self.settled.clear()
            await self._emit(
                frame(
                    "response_start",
                    origin=origin
                    or {"kind": "human" if self.pending else "unclassified"},
                )
            )

    async def can_use_tool(
        self, name: str, inputs: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        # The SDK invokes permissions on a separate task. Wait until the tool
        # card reaches the session consumer, so an approval cannot be recorded
        # against the previous response or disappear in its turn_end cleanup.
        ident = context.tool_use_id
        if ident:
            ready = self.displayed_tools.setdefault(ident, asyncio.Event())
            try:
                await asyncio.wait_for(ready.wait(), 10)
            except TimeoutError:
                return PermissionResultDeny(
                    message="Tool call was not delivered to the UI"
                )
        if self.closed or self.stopping or self.failure:
            return PermissionResultDeny(message="Session is stopping or unavailable")
        kind = tool_kind(name)
        # Both levels reach the approval path: under ``ask`` a write is something
        # the user may grant, not something refused on their behalf. Local reads
        # never arrive here, but WebFetch and WebSearch do under both modes.
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

    async def submit(self, inp: UserTurn) -> None:
        assert self.client
        if self.closed or self.failure:
            raise ValueError(self.failure or "Claude session is closed")
        if self.busy:
            raise ValueError("Claude is still processing background events")
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

        async def prompt() -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "user",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": content},
            }

        self.pending = True
        self.stopping = False
        self.settled.clear()
        try:
            await self.client.query(prompt())
        except BaseException:
            self.pending = False
            self.settled.set()
            raise

    async def run_turn(self, inp: UserTurn) -> AsyncIterator[Frame]:
        """Compatibility for single-turn callers; AgentGUI uses submit/events."""
        await self.submit(inp)
        async with aclosing(self.events()) as events:
            async for value in events:
                if value.type == "response_end":
                    if not self.pending:
                        return
                elif value.type != "response_start":
                    yield value

    async def _receive(self) -> None:
        assert self.client
        streamed_text = False
        streamed_thinking = False
        try:
            async for message in self.client.receive_messages():
                while not self.notices.empty():
                    await self._emit(
                        frame("notice", level="info", text=self.notices.get_nowait())
                    )
                if isinstance(message, SystemMessage):
                    if message.subtype == "init":
                        ident = message.data.get("session_id")
                        if ident:
                            self.confirm_native(ident)
                    elif message.subtype in {
                        "task_started",
                        "task_progress",
                        "task_updated",
                        "task_notification",
                    }:
                        data = message.data
                        patch = data.get("patch", {})
                        status = (
                            data.get("status")
                            or patch.get("status")
                            or message.subtype.removeprefix("task_")
                        )
                        detail = data.get("summary") or data.get("description") or ""
                        await self._emit(
                            frame(
                                "notice",
                                level="warning" if status == "failed" else "info",
                                source="claude_task",
                                task_id=data.get("task_id"),
                                status=status,
                                text=f"Task {data.get('task_id', '')} · {status}"
                                + (f": {detail}" if detail else ""),
                            )
                        )
                elif (
                    isinstance(message, StreamEvent) and not message.parent_tool_use_id
                ):
                    event = message.event
                    if event.get("type") == "message_start":
                        await self._begin_response()
                    delta = event.get("delta", {})
                    if delta.get("type") in {"text_delta", "thinking_delta"}:
                        await self._begin_response()
                        if delta["type"] == "text_delta":
                            streamed_text = True
                            await self._emit(frame("text", delta=delta.get("text", "")))
                        else:
                            streamed_thinking = True
                            await self._emit(
                                frame("thinking", delta=delta.get("thinking", ""))
                            )
                elif isinstance(message, (AssistantMessage, UserMessage)):
                    nested = bool(message.parent_tool_use_id)
                    if isinstance(message, UserMessage) and not nested:
                        text = (
                            message.content
                            if isinstance(message.content, str)
                            else "\n".join(
                                block.text
                                for block in message.content
                                if isinstance(block, TextBlock)
                            )
                        )
                        origin = message.origin
                        if text and origin and origin.get("kind") != "human":
                            await self._begin_response(dict(origin))
                            await self._emit(
                                frame(
                                    "notice",
                                    level="info",
                                    source="claude_event",
                                    origin=dict(origin),
                                    text=text,
                                )
                            )
                        elif text and self.pending:
                            await self._begin_response({"kind": "human"})
                    if isinstance(message.content, str):
                        continue
                    for block in message.content:
                        if (
                            isinstance(block, TextBlock)
                            and isinstance(message, AssistantMessage)
                            and not nested
                        ):
                            await self._begin_response()
                            if not streamed_text:
                                await self._emit(frame("text", delta=block.text))
                        elif isinstance(block, ThinkingBlock) and not nested:
                            await self._begin_response()
                            if not streamed_thinking:
                                await self._emit(
                                    frame("thinking", delta=block.thinking)
                                )
                        elif isinstance(block, ToolUseBlock):
                            if not nested:
                                await self._begin_response()
                            self.displayed_tools.setdefault(block.id, asyncio.Event())
                            await self._emit(
                                frame(
                                    "tool_call",
                                    id=block.id,
                                    name=block.name,
                                    kind=tool_kind(block.name),
                                    arguments=block.input,
                                )
                            )
                        elif isinstance(block, ToolResultBlock):
                            if not nested:
                                await self._begin_response()
                            content = (
                                block.content
                                if isinstance(block.content, str)
                                else json.dumps(block.content, ensure_ascii=False)
                            )
                            await self._emit(
                                frame(
                                    "tool_result",
                                    id=block.tool_use_id,
                                    ok=not block.is_error,
                                    content=content,
                                    attachments=[],
                                )
                            )
                            self.displayed_tools.pop(block.tool_use_id, None)
                    if isinstance(message, AssistantMessage) and not nested:
                        streamed_text = streamed_thinking = False
                elif isinstance(message, ResultMessage):
                    result_origin = dict(message.origin) if message.origin else None
                    await self._begin_response(result_origin)
                    self.confirm_native(message.session_id)
                    usage = message.usage or {}
                    # model_usage and cost are session totals (including
                    # subagents); the flat usage fields describe this turn.
                    await self._emit(
                        usage_frame(
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
                            cache_in_input=False,
                            input=usage.get("input_tokens", 0),
                            output=usage.get("output_tokens", 0),
                            cached=usage.get("cache_read_input_tokens", 0),
                            cache_write=usage.get("cache_creation_input_tokens", 0),
                            cost_usd=message.total_cost_usd,
                        )
                    )
                    if self.stopping or message.terminal_reason in {
                        "aborted_streaming",
                        "aborted_tools",
                    }:
                        await self._emit(frame("cancelled"))
                    elif message.is_error:
                        await self._emit(
                            frame(
                                "error",
                                message=message.result
                                or "; ".join(message.errors or [])
                                or message.subtype,
                            )
                        )
                    else:
                        await self._emit(frame("final", content=message.result or ""))
                    if not result_origin or result_origin.get("kind") == "human":
                        self.pending = False
                    await self._emit(frame("response_end", origin=result_origin))
                    self.responding = False
                    streamed_text = streamed_thinking = False
                    self.settled.set()
            if not self.closed:
                raise RuntimeError("Claude connection closed; reopen this chat")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = str(exc)
            await self._emit(frame("error", message=self.failure))
            if self.responding or self.pending:
                await self._emit(frame("response_end"))
        finally:
            self.responding = self.pending = False
            self.settled.set()
            for ready in self.displayed_tools.values():
                ready.set()
            if not self.closed:
                await self.outgoing.put(None)

    async def stop(self) -> list[Frame]:
        if not self.client or not (self.responding or self.pending):
            return []
        self.stopping = True
        try:
            async with asyncio.timeout(10):
                await self.client.interrupt()
                await self.settled.wait()
                await self.outgoing.join()
            return []
        except TimeoutError:
            await self.close()
            return [
                frame("cancelled"),
                frame(
                    "error",
                    message="Claude did not finish interrupting; reopen this chat.",
                ),
            ]
        finally:
            self.stopping = False

    async def fork(self, through_seq: int | None) -> SessionRecord:
        self.require_tip(through_seq)
        options = {**self.session.options, "_fork_pending": True}
        # Claude materializes the new native id on its first query. The marker
        # survives reloads until init/result confirms that new id.
        return self.store.clone(self.session, self.session.native_id, options=options)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for ready in self.displayed_tools.values():
            ready.set()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.client:
            await self.client.disconnect()
