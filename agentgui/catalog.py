"""Explicit model choices; never route a chat automatically."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BACKENDS = frozenset({"claude", "codex", "lingcore"})

DEFAULT_CATALOG = """[[model]]
id = "claude-opus"
backend = "claude"
native = "opus"
label = "Claude Opus (Claude Code)"

[[model]]
id = "claude-sonnet"
backend = "claude"
native = "sonnet"
label = "Claude Sonnet (Claude Code)"

[[model]]
id = "gpt-codex"
backend = "codex"
native = ""
label = "Codex default"

[[model]]
id = "cheap-deepseek"
backend = "lingcore"
profile = "~/LingCore/profiles/coding"
label = "LingCore · cost-effective"
"""


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str
    backend: str
    native_model: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        # Like SessionRecord.to_wire: options stay server-side.
        result = asdict(self)
        result.pop("options")
        return result


def config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "agentgui/models.toml"
    )


class Catalog:
    def __init__(self, entries: list[ModelEntry]) -> None:
        self.entries = {entry.id: entry for entry in entries}
        if (
            not entries
            or any(not entry.id or entry.backend not in BACKENDS for entry in entries)
            or len(entries) != len(self.entries)
        ):
            raise ValueError("catalog must contain unique, nonempty model entries")

    @classmethod
    def load(cls, path: str | Path | None = None) -> Catalog:
        target = Path(path).expanduser() if path else config_path()
        if not target.exists() and path is None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(DEFAULT_CATALOG, encoding="utf-8")
            except OSError:
                return cls.load_default()
        return cls.parse(target.read_text(encoding="utf-8"), target.parent)

    @classmethod
    def load_default(cls) -> Catalog:
        return cls.parse(DEFAULT_CATALOG, config_path().parent)

    @classmethod
    def parse(cls, text: str, base: Path) -> Catalog:
        """Parse catalog TOML; relative LingCore profiles resolve against ``base``."""
        entries = []
        for raw in tomllib.loads(text).get("model", []):
            if not isinstance(raw, dict):
                raise ValueError("each model must be a TOML table")
            ident, backend = raw.get("id"), raw.get("backend")
            if not isinstance(ident, str) or not ident or backend not in BACKENDS:
                raise ValueError(
                    "each model requires an id and claude/codex/lingcore backend"
                )
            options = raw.get("options", {})
            if not isinstance(options, dict):
                raise ValueError(f"{ident}: options must be a TOML table")
            options = dict(options)
            if backend == "lingcore":
                profile = raw.get("profile")
                if not isinstance(profile, str) or not profile:
                    raise ValueError(f"{ident}: LingCore requires a profile")
                profile_path = Path(profile).expanduser()
                if not profile_path.is_absolute():
                    profile_path = base / profile_path
                options["profile"] = str(profile_path.resolve())
            native, label = raw.get("native", ""), raw.get("label", ident)
            if not isinstance(native, str) or not isinstance(label, str):
                raise ValueError("model native and label must be strings")
            entries.append(ModelEntry(ident, label, backend, native, options))
        return cls(entries)
