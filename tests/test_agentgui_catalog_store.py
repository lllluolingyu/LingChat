from __future__ import annotations

from pathlib import Path

from agentgui.catalog import Catalog, ModelEntry
from agentgui.store import Store


def test_default_catalog_has_explicit_backends(tmp_path: Path):
    catalog = Catalog.load_default()
    assert {e.backend for e in catalog.entries.values()} == {
        "claude",
        "codex",
        "lingcore",
    }


def test_store_persists_turns_and_forks(tmp_path: Path):
    catalog = Catalog.load_default()
    store = Store(tmp_path / "sessions.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rec = store.create(catalog.entries["gpt-codex"], str(workspace))
    store.put_turn(rec.id, 0, "user", [{"type": "user", "text": "hello"}])
    store.append_frame(rec.id, 0, {"type": "text", "delta": "hi"})
    assert store.turns(rec.id)[0]["frames"][-1]["delta"] == "hi"
    fork = store.clone(rec, "native-child", 0)
    assert fork.parent_id == rec.id
    assert store.turns(fork.id)[0]["seq"] == 0
    [listed] = [s for s in store.list_sessions() if s["id"] == rec.id]
    assert listed["message_count"] == 1 and "options" not in listed
    store.close()


def test_store_coalesces_consecutive_deltas(tmp_path: Path):
    store = Store(tmp_path / "sessions.db")
    rec = store.create(Catalog.load_default().entries["gpt-codex"], str(tmp_path))
    store.put_turn(rec.id, 1, "assistant", [])
    for msg in [
        {"type": "thinking", "delta": "a"},
        {"type": "thinking", "delta": "b"},
        {"type": "text", "delta": "he"},
        {"type": "text", "delta": "llo"},
        {"type": "tool_call", "id": "t"},
        {"type": "text", "delta": "!"},
    ]:
        store.append_frame(rec.id, 1, msg)
    store.append_frame(rec.id, 7, {"type": "text", "delta": "no row"})
    assert store.turns(rec.id)[0]["frames"] == [
        {"type": "thinking", "delta": "ab"},
        {"type": "text", "delta": "hello"},
        {"type": "tool_call", "id": "t"},
        {"type": "text", "delta": "!"},
    ]
    store.close()


def test_legacy_autonomy_rows_are_renamed_on_open(tmp_path: Path):
    """Left alone, a legacy row would not error -- it would quietly get more.

    Codex and LingCore compare autonomy with ``==`` and fall through to their
    permissive branch, so an unrenamed ``read-only`` row would earn a
    workspace-write sandbox. Only Claude would fail loudly.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    entry = ModelEntry("codex", "Codex", "codex")
    stale = store.create(entry, str(workspace), "ask")
    modern = store.create(entry, str(workspace), "edit")
    with store.db:
        store.db.execute(
            "UPDATE sessions SET autonomy = 'read-only' WHERE id = ?", (stale.id,)
        )
        store.db.execute(
            "UPDATE sessions SET autonomy = 'auto-edit' WHERE id = ?", (modern.id,)
        )
    store.close()

    store = Store(tmp_path / "gui.db")
    try:
        assert store.get(stale.id).autonomy == "ask"
        assert store.get(modern.id).autonomy == "edit"
    finally:
        store.close()
