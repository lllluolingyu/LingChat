"""Static-asset smoke tests.

The browser UI is served straight from ``web/`` (force-included into the
wheel as ``lingchat/web`` — see pyproject.toml). A renamed or dropped file
breaks the app only at page load in a browser, which no Python test would
otherwise notice — so assert that every asset ``index.html`` references is
actually served, and that both UIs' module graphs (including ``/shared/``)
resolve through their servers.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

from starlette.testclient import TestClient

from lingchat.server import _WEB_DIR, create_app


def _write_profile(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"name: test\nworkspace: {ws}\nllm:\n  model: fake\ntools: []\n",
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


def test_stylesheet_url_references_resolve():
    """Self-hosted fonts are referenced from CSS, which no page-load test sees.

    A renamed or dropped ``.woff2`` degrades silently to a system fallback, so
    check both browser UIs against their own directories.
    """
    import agentgui

    for web in (_WEB_DIR, Path(agentgui.__file__).with_name("web")):
        css = (web / "style.css").read_text(encoding="utf-8")
        refs = re.findall(r'url\(["\']?([^"\')]+)["\']?\)', css)
        assert refs, f"no url() references found in {web}/style.css"
        for ref in refs:
            assert (web / ref).is_file(), (
                f"{web.name}/style.css references missing {ref}"
            )


def test_es_module_imports_resolve(tmp_path, app_factory):
    """Follow browser URLs, including the common /shared mount, on both apps."""
    legacy = create_app(_write_profile(tmp_path), require_auth=False)
    gui, _, _ = app_factory()
    for app in (legacy, gui):
        with TestClient(app) as client:
            seen = set()
            pending = ["/js/main.js", "/js/bootstrap.js"]
            while pending:
                path = pending.pop()
                if path in seen:
                    continue
                seen.add(path)
                response = client.get(path)
                assert response.status_code == 200, path
                assert "javascript" in response.headers["content-type"], path
                for spec in re.findall(r'from\s+"([./][^"]+)"', response.text):
                    pending.append(urljoin(path, spec))
            assert "/shared/markdown.js" in seen
            assert "/shared/command-menu.js" in seen
            assert "/shared/stream-view.js" in seen


def test_browser_policy_and_external_bootstrap(tmp_path, app_factory):
    legacy = create_app(_write_profile(tmp_path), require_auth=False)
    gui, _, _ = app_factory()
    for app in (legacy, gui):
        with TestClient(app) as client:
            for path in (
                "/",
                "/js/main.js",
                "/shared/markdown.js",
                "/missing.js",
                "/docs/missing",
                "/openapi.json",
            ):
                response = client.get(path)
                policy = response.headers["content-security-policy"]
                assert "script-src 'self'" in policy
                assert "img-src 'self' data:" in policy
                assert "connect-src 'self'" in policy
                assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
                assert response.headers["x-content-type-options"] == "nosniff"
                assert response.headers["cache-control"] == "no-store"
            index = client.get("/").text
            assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index)
            assert '<script src="/js/bootstrap.js"></script>' in index
            assert "localStorage.getItem(" in client.get("/js/bootstrap.js").text


def test_generated_documentation_is_exempt_from_browser_csp(tmp_path, app_factory):
    legacy = create_app(_write_profile(tmp_path), require_auth=False)
    gui, _, _ = app_factory()
    for app in (legacy, gui):
        with TestClient(app) as client:
            for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
                response = client.get(path)
                assert response.status_code == 200, path
                assert "text/html" in response.headers["content-type"], path
                assert "<script" in response.text, path
                assert "content-security-policy" not in response.headers, path
                assert response.headers["x-content-type-options"] == "nosniff"
                assert response.headers["referrer-policy"] == "no-referrer"
                assert response.headers["cache-control"] == "no-store"


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


def test_attachment_and_confirmation_parity_controls_are_wired():
    index = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    connection = (_WEB_DIR / "js" / "connection.js").read_text(encoding="utf-8")
    main = (_WEB_DIR / "js" / "main.js").read_text(encoding="utf-8")
    thread = (_WEB_DIR / "js" / "thread.js").read_text(encoding="utf-8")

    file_input = re.search(r'<input\b[^>]*\bid="file-input"[^>]*>', index)
    assert file_input is not None
    assert "accept=" not in file_input.group(0)
    assert 'id="confirm-allow-session"' in index
    assert 'id="confirm-runner"' in index

    assert "msg.limits" in main
    assert 'case "shell_allowlist"' in main
    from lingchat._web import SHARED_DIR

    assert "displayKind" in (SHARED_DIR / "attachments.js").read_text()
    assert "createAttachmentQueue" in main
    assert "id: item.id, approved, scope" in connection
    assert 'answerConfirm(true, "session")' in connection
    assert "msg.allowlist_pattern" in main
    assert "runner" in connection

    assert 'text: "TXT"' in thread
    assert 'binary: "BIN"' in thread
    assert "attachment-download" in thread
    assert "attachmentDownloadHandler" in thread


def test_durable_runtime_events_are_replayed_with_history():
    sessions = (_WEB_DIR / "js" / "sessions.js").read_text(encoding="utf-8")

    assert "data.events || []" in sessions
    assert 'event.type === "compact"' in sessions
    assert 'event.type === "skill_state"' in sessions
    assert 'event.type === "todos"' in sessions
    assert "compactNote(event)" in sessions
    assert "byMessage.get(m.seq)" in sessions


def test_gpt_models_get_their_own_mark():
    """A GPT-series model is identified by its own glyph, not the generic one.

    The picker, the session badge and the avatar all draw from one helper, so
    check the helper's routing and that each surface actually calls it.
    """

    import agentgui

    web = Path(agentgui.__file__).with_name("web")
    marks = (web / "js" / "marks.js").read_text(encoding="utf-8")
    thread = (web / "js" / "thread.js").read_text(encoding="utf-8")
    sessions = (web / "js" / "sessions.js").read_text(encoding="utf-8")
    index = (web / "index.html").read_text(encoding="utf-8")

    # Codex only ever serves OpenAI models; a LingCore profile fronting one is
    # recognised by name instead.
    assert 'model.backend === "codex"' in marks
    assert "GPT_MARK" in marks and "AGENT_MARK" in marks
    # Both marks are drawn in currentColor, so one glyph works on the clay disc
    # and in a hairline badge without a second copy.
    assert marks.count('stroke="currentColor"') >= 2

    # The avatar and empty state follow the backend from "hello"...
    assert "markFor(" in thread and "agentMark" in thread
    assert "AVATAR_SVG" not in thread, "the fixed avatar constant should be gone"
    # ...the sidebar badge and the picker resolve it per entry.
    assert "markFor(s.backend" in sessions
    assert "syncModelMark" in sessions
    assert 'id="new-model-mark"' in index
