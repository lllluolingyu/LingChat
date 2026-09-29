# Derived from LingChat; Copyright LingChat contributors; Apache-2.0.
"""LingCore bridge, preserving its native cancellation and history semantics."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

from lingcore.agent import Agent
from lingcore.config import AgentProfile
from lingcore.events import (
    Compacted,
    Error,
    Final,
    SkillActivated,
    StreamRetry,
    TextDelta,
    ToolCallStarted,
    ToolResultEvent,
    TurnCancelled,
    UsageReported,
)
from lingcore.message import UserInput
from lingcore.sessions import SessionStore, new_session_id, open_store
from lingcore.tools.builtin.shell import allowlist_pattern_for

from agentgui._compat import TodoUpdated, todos_from_payload
from agentgui.attachments import attachment_payloads
from agentgui.protocol import ApprovalRequest, Frame, frame, tool_kind
from agentgui.store import SessionRecord, Store
from agentgui.usage import model_usage, usage_frame

from .base import ApproveFn, BackendBase, Capabilities, UserTurn


class ProfileCache:
    def __init__(self) -> None:
        self.loaded: dict[str, tuple[AgentProfile, SessionStore | None]] = {}

    def get(self, path: str) -> tuple[AgentProfile, SessionStore | None]:
        key = str(Path(path).expanduser().resolve())
        if key not in self.loaded:
            profile = AgentProfile.load(key)
            store, notice = open_store(profile)
            if notice:
                raise ValueError(notice)
            self.loaded[key] = profile, store
        return self.loaded[key]

    def close(self) -> None:
        for _, store in self.loaded.values():
            if store:
                store.close()


def event_frame(event: Any) -> Frame:
    match event:
        case TextDelta(text):
            return frame("text", delta=text)
        case ToolCallStarted(call):
            return frame(
                "tool_call",
                id=call.id,
                name=call.name,
                kind=tool_kind(call.name),
                arguments=call.arguments,
            )
        case ToolResultEvent(result):
            return frame(
                "tool_result",
                id=result.call_id,
                name=result.name,
                ok=result.ok,
                content=result.content,
                attachments=attachment_payloads(result.attachments),
            )
        case Final(content):
            return frame("final", content=content)
        case Error(message):
            return frame("error", message=message)
        case TurnCancelled(reason):
            return frame("cancelled", reason=reason)
        case TodoUpdated(todos):
            return frame("todos", todos=[item.model_dump() for item in todos])
        case SkillActivated(name, active):
            return frame(
                "notice",
                level="info",
                text=f"Skill {'activated' if active else 'deactivated'}: {name}",
            )
        case Compacted(count, before, after):
            return frame(
                "notice",
                level="info",
                text=f"Context compacted: {count} messages ({before} → {after} tokens).",
            )
        case UsageReported(usage):
            # One model request (reply, summarizer, or vision fallback);
            # LingCore reports only what the provider returned.
            return usage_frame(
                [
                    model_usage(
                        usage.model,
                        input=usage.input_tokens,
                        output=usage.output_tokens,
                        cached=usage.cached_input_tokens,
                        reasoning=usage.reasoning_tokens,
                    )
                ],
                scope="request",
                cumulative=False,
                input=usage.input_tokens,
                output=usage.output_tokens,
                cached=usage.cached_input_tokens,
            )
        case StreamRetry(attempt, maximum, reason, discarded):
            return frame(
                "notice",
                level="warning",
                text=f"{reason}; retrying ({attempt}/{maximum})",
                discarded_chars=discarded,
            )
    return frame("notice", level="info", text=str(event))


class LingCoreBackend(BackendBase):
    capabilities = Capabilities(
        edit_regenerate=True,
        fork=True,
        images=True,
        files=True,
        thinking=True,
        fork_at_message=True,
    )

    def __init__(
        self,
        store: Store,
        cache: ProfileCache,
        llm_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(store)
        self.cache, self.llm_factory = cache, llm_factory
        self.task: asyncio.Task[Any] | None = None
        self.agent: Agent | None = None
        self.native: SessionStore | None = None
        self.profile: AgentProfile
        self.options: dict[str, Any]
        self.shell_commands: set[str] = set()

    async def start(self, session: SessionRecord, approve: ApproveFn) -> None:
        await super().start(session, approve)
        profile, self.native = self.cache.get(session.options["profile"])
        self.profile = profile.model_copy(deep=True)
        self.profile.workspace = session.workspace
        if session.autonomy == "ask":
            # LingCore has no per-write approval hook: ``ctx.confirm`` is reached
            # only from run_shell, skill gating and subagent spawn, never from
            # write_file/edit_file/patch_file. So "ask before writing" has to be a
            # tool ceiling, with run_shell kept as the one approvable way to act.
            safe = {
                "read_file",
                "list_dir",
                "search",
                "web_search",
                "fetch_url",
                "run_shell",
                # Only rewrites the agent's own in-memory checklist.
                "todo_write",
            }
            self.profile.tools = [name for name in self.profile.tools if name in safe]
            # Skills stay: ``SkillState.effective_tools`` is ceiling ∩ requested, so
            # no skill can grant past the list above, and this is the default mode.
            # ``initial_tools`` is taken verbatim when set, so clear it to mean "the
            # whole filtered ceiling"; clearing ``skill_gated_tools`` overrides
            # profile intent deliberately, since a profile that gates run_shell
            # behind a skill would otherwise leave this mode no way to act at all.
            self.profile.initial_tools = None
            self.profile.skill_gated_tools = []
        self.options = copy.deepcopy(self.profile.tool_options)
        if "run_shell" in self.profile.tools:
            self.options.setdefault("run_shell", {})["require_confirmation"] = True
            if session.autonomy == "ask":
                self.options["run_shell"]["allow_patterns"] = []
        # Edit and fork are native session-store operations.
        self.capabilities = replace(
            self.capabilities,
            edit_regenerate=self.native is not None,
            fork=self.native is not None,
            fork_at_message=self.native is not None,
        )
        if not session.native_id:
            self.save_native(new_session_id())
        self.agent = self._build()
        self.sync_history()

    def _build(self, llm: Any = None) -> Agent:
        return Agent.from_profile(
            self.profile,
            confirm=self.confirm,
            base_dir=Path(self.session.workspace),
            tool_options=self.options,
            llm=llm or (self.llm_factory() if self.llm_factory else None),
            session_store=self.native,
            session_id=self.session.native_id,
        )

    async def confirm(self, prompt: str) -> bool:
        # No level refuses on the user's behalf any more: under ``ask`` the empty
        # allow-list means every command arrives here for them to decide.
        shell = prompt in self.shell_commands
        pattern = allowlist_pattern_for(prompt) if shell else None
        answer = await self.approve(
            ApprovalRequest(
                "shell" if shell else "other",
                "Run shell command?" if shell else "Approve tool action?",
                prompt,
                options=["once", "session", "deny"] if pattern else ["once", "deny"],
            )
        )
        if answer == "session" and pattern:
            patterns = self.options.setdefault("run_shell", {}).setdefault(
                "allow_patterns", []
            )
            if pattern not in patterns:
                patterns.append(pattern)
        return answer != "deny"

    async def run_turn(self, inp: UserTurn) -> AsyncIterator[Frame]:
        assert self.agent
        self.task = asyncio.current_task()
        self.shell_commands.clear()
        try:
            async with aclosing(
                self.agent.run(UserInput(text=inp.text, attachments=inp.attachments))
            ) as stream:
                async for event in stream:
                    if (
                        isinstance(event, ToolCallStarted)
                        and event.call.name == "run_shell"
                    ):
                        command = event.call.arguments.get("command")
                        if isinstance(command, str):
                            self.shell_commands.add(command)
                    yield event_frame(event)
        finally:
            self.task = None
            self.sync_history()

    async def stop(self) -> list[Frame]:
        assert self.agent
        task = self.task
        started = self.agent.cancel_turn()
        if task and task is not asyncio.current_task():
            if not started:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        result = self.agent.finalize_cancelled_turn() if started else TurnCancelled()
        self.sync_history()
        # Requests that finished before the cancellation landed were billed.
        return [event_frame(u) for u in self.agent.drain_usage()] + [
            event_frame(result)
        ]

    def reconcile(self, status: list[dict[str, Any]]) -> None:
        # Reconcile after native cancellation finalization, then retain status
        # information that LingCore's canonical messages do not carry.
        self.sync_history()
        self.store.append_status(self.session.id, status)

    def sync_history(self) -> None:
        if not self.native or not self.session.native_id:
            return
        old_status = {
            t["seq"]: [
                f for f in t["frames"] if f["type"] in {"error", "cancelled", "notice"}
            ]
            for t in self.store.turns(self.session.id)
        }
        turns = []
        events = self.native.events(self.session.native_id)
        for record in self.native.message_records(self.session.native_id):
            m = record.message
            frames: list[dict[str, Any]] = []
            if m.role == "user":
                frames.append(
                    {
                        "type": "user",
                        "text": m.input_text if m.input_text is not None else m.content,
                        "name": m.name,
                        "attachments": [
                            a.model_dump(exclude={"fallback_text"})
                            for a in m.attachments
                        ],
                    }
                )
            elif m.role == "assistant":
                if m.content:
                    frames.append({"type": "text", "delta": m.content})
                frames.extend(
                    {
                        "type": "tool_call",
                        "id": tc.id,
                        "name": tc.name,
                        "kind": tool_kind(tc.name),
                        "arguments": tc.arguments,
                    }
                    for tc in m.tool_calls
                )
            else:
                frames.append(
                    {
                        "type": "tool_result",
                        "id": m.tool_call_id,
                        "name": m.name,
                        "ok": not m.content.startswith("ERROR: "),
                        "content": m.content,
                    }
                )
            for event in events:
                if event.message_seq != record.seq:
                    continue
                if event.kind == "todo_state":
                    todos = todos_from_payload(event.payload)
                    if todos is not None:
                        frames.append(
                            {
                                "type": "todos",
                                "todos": [item.model_dump() for item in todos],
                            }
                        )
                    continue
                frames.append(
                    {
                        "type": "notice",
                        "level": "info",
                        "text": f"{event.kind}: {event.payload}",
                    }
                )
            frames.extend(f for f in old_status.get(record.seq, []) if f not in frames)
            turns.append({"seq": record.seq, "role": m.role, "frames": frames})
        self.store.replace_turns(self.session.id, turns)

    async def edit(self, seq: int, text: str) -> AsyncIterator[Frame]:
        if not self.native or not self.agent or not self.session.native_id:
            raise ValueError("editing requires native session history")
        original = self.native.rewind_to_user_message(self.session.native_id, seq)
        self.agent = self._build(self.agent.llm)
        self.store.truncate(self.session.id, seq)
        yield frame("edit_accepted", seq=seq, text=text)
        async for event in self.run_turn(UserTurn(text, original.attachments)):
            yield event

    async def fork(self, through_seq: int | None) -> SessionRecord:
        if not self.native or not self.session.native_id:
            raise ValueError("forking requires native session history")
        child = self.native.fork_session(
            self.session.native_id, through_seq=through_seq
        )
        return self.store.clone(self.session, child.id, through_seq)

    async def close(self) -> None:
        if self.task:
            await self.stop()
