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
