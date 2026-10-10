"""Backend-neutral wire frames. Only explicit approval decisions grant access."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import uuid4

Decision = Literal["once", "session", "deny"]


@dataclass(slots=True)
class Frame:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {**self.data, "type": self.type}


def frame(type_: str, **data: Any) -> Frame:
    return Frame(type_, data)


def decision(value: object) -> Decision:
    if value == "once":
        return "once"
    if value == "session":
        return "session"
    return "deny"


@dataclass(slots=True)
class ApprovalRequest:
    kind: str
    title: str
    detail: str
    diff: str | None = None
    options: list[str] = field(default_factory=lambda: ["once", "session", "deny"])
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_wire(self) -> dict[str, Any]:
        return {"type": "approval", **asdict(self)}


def tool_kind(name: str) -> str:
    key = name.lower()
    if key.startswith("mcp__") or key == "mcptoolcall":
        return "mcp"
    if key in {"bash", "run_shell", "commandexecution", "shell"}:
        return "shell"
    if key in {
        "edit",
        "write",
        "multiedit",
        "filechange",
        "write_file",
        "edit_file",
        "apply_patch",
        "patch_file",
    }:
        return "edit"
    if key in {"read", "read_file", "list_dir"}:
        return "read"
    if key in {"glob", "grep", "search", "search_files"}:
        return "search"
    if key in {"webfetch", "websearch", "websearchcall", "web_search", "fetch_url"}:
        return "web"
    return "other"


def plugin_notice(plugin: str, hook: str, action: str, message: str) -> Frame:
    """Visible plugin policy event shared with LingCore frontends."""
    return frame(
        "plugin_notice", plugin=plugin, hook=hook, action=action, message=message
    )
