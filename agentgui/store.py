"""GUI display history. Native histories stay owned by their agent backend."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import uuid4

from .catalog import ModelEntry

# Consecutive streaming deltas of one kind are stored as a single frame.
_DELTA_TYPES = {"text", "thinking"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "agentgui/sessions.db"
    )


# What the agent may do *without* asking. ``ask`` reads freely and raises an
# approval for every write or command; ``edit`` writes inside the workspace
# silently and still asks to step outside it. Consumers import these rather than
# copying the values, so a level cannot be accepted by one layer and rejected by
# the next -- typing a request model with ``Autonomy`` earns the rejection for
# free, and ``get_args`` keeps the runtime set from drifting from the type.
Autonomy = Literal["ask", "edit"]
AUTONOMY_LEVELS: frozenset[str] = frozenset(get_args(Autonomy))


@dataclass
class SessionRecord:
    id: str
    backend: str
    model_id: str
    workspace: str
    # True by construction, not by enforcement: ``_session`` hydrates rows
    # unvalidated, so the DDL rename above is what keeps old rows honest.
    autonomy: Autonomy
    title: str
    native_id: str | None
    parent_id: str | None
    created_at: str
    updated_at: str
    native_model: str
    options: dict[str, Any]
    model_label: str

    def to_wire(self) -> dict[str, Any]:
        result = asdict(self)
        # Profiles can contain provider settings; only expose the display metadata.
        result.pop("options")
        return result


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        target = Path(path) if path is not None else state_path()
        fallback = Path(tempfile.gettempdir()) / "agentgui/sessions.db"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            target = fallback
        try:
            self.db = sqlite3.connect(target, check_same_thread=False)
        except (OSError, sqlite3.Error):
            if target == fallback:
                raise
            fallback.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(fallback, check_same_thread=False)
            target = fallback
        try:
            target.chmod(0o600)
        except OSError:
            # An existing read-only database can still be safely opened when
            # its mode cannot be changed by this process.
            pass
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, backend TEXT NOT NULL, model_id TEXT NOT NULL,
                workspace TEXT NOT NULL, autonomy TEXT NOT NULL, title TEXT NOT NULL,
                native_id TEXT, parent_id TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, native_model TEXT NOT NULL,
                options TEXT NOT NULL, model_label TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
                seq INTEGER, role TEXT NOT NULL, frames_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(session_id, seq)
            );
            DROP TABLE IF EXISTS wire_events;
            -- Rows predating the two-level model. Renaming them is not cosmetic:
            -- the backends test autonomy with ``==`` and fall through to their
            -- permissive branch, so an unrenamed ``read-only`` row would quietly
            -- earn a workspace-write sandbox instead of erroring.
            UPDATE sessions SET autonomy = 'ask' WHERE autonomy = 'read-only';
            UPDATE sessions SET autonomy = 'edit' WHERE autonomy = 'auto-edit';
        """)

    def close(self) -> None:
        self.db.close()

    def create(
        self,
        entry: ModelEntry,
        workspace: str,
        autonomy: Autonomy = "ask",
        *,
        native_id: str | None = None,
        parent_id: str | None = None,
        title: str = "New chat",
    ) -> SessionRecord:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("workspace must be an existing directory")
        if autonomy not in AUTONOMY_LEVELS:
            raise ValueError("invalid autonomy level")
        stamp = now()
        rec = SessionRecord(
            uuid4().hex,
            entry.backend,
            entry.id,
            str(root),
            autonomy,
            title,
            native_id,
            parent_id,
            stamp,
            stamp,
            entry.native_model,
            deepcopy(entry.options),
            entry.label,
        )
        values = asdict(rec)
        values["options"] = json.dumps(rec.options)
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(values.values()),
            )
        return rec

    def get(self, sid: str) -> SessionRecord | None:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return None if row is None else _session(row)

    def save(self, rec: SessionRecord) -> None:
        rec.updated_at = now()
        with self.db:
            self.db.execute(
                "UPDATE sessions SET title=?, native_id=?, options=?, updated_at=? WHERE id=?",
                (
                    rec.title,
                    rec.native_id,
                    json.dumps(rec.options),
                    rec.updated_at,
                    rec.id,
                ),
            )

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT s.*, (SELECT count(*) FROM turns WHERE session_id=s.id) AS n"
            " FROM sessions s ORDER BY updated_at DESC"
        )
        return [
            {**_session(row, frozenset({"n"})).to_wire(), "message_count": row["n"]}
            for row in rows
        ]

    def next_seq(self, sid: str) -> int:
        return int(
            self.db.execute(
                "SELECT coalesce(max(seq), -1)+1 FROM turns WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )

    def put_turn(
        self, sid: str, seq: int, role: str, frames: list[dict[str, Any]]
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?)",
                (sid, seq, role, json.dumps(frames), now()),
            )
            self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))

    def append_frame(self, sid: str, seq: int, msg: dict[str, Any]) -> None:
        """Append a transcript frame, merging it into a preceding same-kind delta."""
        with self.db:
            row = self.db.execute(
                "SELECT frames_json FROM turns WHERE session_id=? AND seq=?",
                (sid, seq),
            ).fetchone()
            if row is None:
                return
            frames = json.loads(row[0])
            last = frames[-1] if frames else None
            if (
                last
                and msg["type"] in _DELTA_TYPES
                and last["type"] == msg["type"]
                and last.keys() == msg.keys() == {"type", "delta"}
            ):
                last["delta"] += msg["delta"]
            else:
                frames.append(msg)
            self.db.execute(
                "UPDATE turns SET frames_json=? WHERE session_id=? AND seq=?",
                (json.dumps(frames), sid, seq),
            )

    def turns(self, sid: str) -> list[dict[str, Any]]:
        return [
            {"seq": r["seq"], "role": r["role"], "frames": json.loads(r["frames_json"])}
            for r in self.db.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY seq", (sid,)
            )
        ]

    def truncate(self, sid: str, seq: int) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM turns WHERE session_id=? AND seq>=?", (sid, seq)
            )

    def replace_turns(self, sid: str, turns: list[dict[str, Any]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
            self.db.executemany(
                "INSERT INTO turns VALUES (?,?,?,?,?)",
                [
                    (sid, t["seq"], t["role"], json.dumps(t["frames"]), now())
                    for t in turns
                ],
            )

    def append_status(self, sid: str, frames: list[dict[str, Any]]) -> None:
        turns = self.turns(sid)
        if not frames or not turns:
            return
        last = turns[-1]
        last["frames"].extend(f for f in frames if f not in last["frames"])
        self.put_turn(sid, last["seq"], last["role"], last["frames"])

    def clone(
        self,
        rec: SessionRecord,
        native_id: str | None,
        through_seq: int | None = None,
        *,
        options: dict[str, Any] | None = None,
    ) -> SessionRecord:
        entry = ModelEntry(
            rec.model_id,
            rec.model_label,
            rec.backend,
            rec.native_model,
            options if options is not None else rec.options,
        )
        child = self.create(
            entry,
            rec.workspace,
            rec.autonomy,
            native_id=native_id,
            parent_id=rec.id,
            title=rec.title + " (fork)",
        )
        turns = [
            t
            for t in self.turns(rec.id)
            if through_seq is None or t["seq"] <= through_seq
        ]
        self.replace_turns(child.id, turns)
        return child

    def delete(self, sid: str) -> bool:
        with self.db:
            return (
                self.db.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount > 0
            )


def _session(row: sqlite3.Row, exclude: frozenset[str] = frozenset()) -> SessionRecord:
    raw = {k: row[k] for k in row.keys() if k not in exclude}
    raw["options"] = json.loads(raw["options"])
    return SessionRecord(**raw)
