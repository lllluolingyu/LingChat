"""Static-asset smoke tests.

The browser UI is served straight from ``web/`` (force-included into the
wheel as ``lingchat/web`` — see pyproject.toml). A renamed or dropped file
breaks the app only at page load in a browser, which no Python test would
otherwise notice — so assert that every asset ``index.html`` references is
actually served, and that every relative ES-module import inside ``web/js/``
points at a file that exists.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

from lingchat.server import _WEB_DIR, create_app


def _write_profile(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: test\n"
        f"workspace: {ws}\n"
        "llm:\n"
        "  model: fake\n"
        "tools: []\n",
        encoding="utf-8",
    )
    return cfg


def test_index_asset_references_are_served(tmp_path):
    app = create_app(_write_profile(tmp_path), require_auth=False)
    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200
    # Root-relative src/href references only; the data: favicon is exempt.
    refs = re.findall(r'(?:src|href)="(/[^"]+)"', index.text)
    assert "/js/main.js" in refs
    assert "/style.css" in refs
    for ref in refs:
        assert client.get(ref).status_code == 200, (
            f"{ref} is referenced by index.html but not served"
        )


def test_es_module_imports_resolve():
    js_dir = _WEB_DIR / "js"
    modules = sorted(js_dir.glob("*.js"))
    assert modules, "no ES modules found under web/js/"
    for mod in modules:
        text = mod.read_text(encoding="utf-8")
        for spec in re.findall(r'from\s+"(\.{1,2}/[^"]+)"', text):
            target = (mod.parent / spec).resolve()
            assert target.is_file(), f"{mod.name} imports missing module {spec}"


def test_stop_edit_and_fork_controls_are_wired():
    index = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    connection = (_WEB_DIR / "js" / "connection.js").read_text(encoding="utf-8")
    main = (_WEB_DIR / "js" / "main.js").read_text(encoding="utf-8")
    sessions = (_WEB_DIR / "js" / "sessions.js").read_text(encoding="utf-8")
    thread = (_WEB_DIR / "js" / "thread.js").read_text(encoding="utf-8")

    assert 'id="stop"' in index and "Stop response" in index
    assert 'type: "stop"' in main
    assert 'type: "edit"' in main
    assert "setEditHandler" in thread
    assert "Save & regenerate" in thread
    assert "setForkHandler" in main and "setForkHandler" in thread
    assert "/fork" in sessions and "takePendingForkEdit" in sessions
    assert "Fork and regenerate from this message" in thread
    assert "WebSocket.CONNECTING" in connection


def test_durable_runtime_events_are_replayed_with_history():
    sessions = (_WEB_DIR / "js" / "sessions.js").read_text(encoding="utf-8")

    assert "data.events || []" in sessions
    assert 'event.type === "compact"' in sessions
    assert 'event.type === "skill_state"' in sessions
    assert "compactNote(event)" in sessions
    assert "byMessage.get(m.seq)" in sessions
