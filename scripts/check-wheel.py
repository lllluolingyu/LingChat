"""Smoke test an installed LingChat wheel with ``python -I check-wheel.py``.

Run outside the source checkout. No provider credentials or native agent CLIs
are needed: import both servers and serve their packaged browser assets.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urljoin

from starlette.testclient import TestClient

import agentgui
import lingchat
from agentgui.catalog import Catalog, ModelEntry
from agentgui.server import create_app as create_gui
from lingchat.server import create_app as create_legacy


def main() -> None:
    for package in (lingchat, agentgui):
        assert package.__file__ is not None
        assert (
            Path(package.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        ), f"{package.__name__} was imported from a checkout, not the installed wheel"
        subprocess.run(
            [sys.executable, "-I", "-m", package.__name__, "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

    with tempfile.TemporaryDirectory(prefix="lingchat-wheel-") as directory:
        root = Path(directory)
        profile = root / "config.yaml"
        profile.write_text(
            f"name: wheel-check\nworkspace: {root.as_posix()}\n"
            "llm:\n  model: fake\ntools: []\nsessions:\n  enabled: false\n",
            encoding="utf-8",
        )
        catalog = Catalog(
            [ModelEntry("fake", "Fake", "lingcore", options={"profile": str(profile)})]
        )
        apps = (
            create_legacy(profile, require_auth=False),
            create_gui(catalog=catalog, store_path=root / "gui.db", require_auth=False),
        )
        for app in apps:
            with TestClient(app) as client:
                index = client.get("/")
                assert index.status_code == 200
                pending = re.findall(r'(?:src|href)="(/[^\"]+)"', index.text)
                seen: set[str] = set()
                while pending:
                    path = pending.pop()
                    if path in seen:
                        continue
                    seen.add(path)
                    response = client.get(path)
                    assert response.status_code == 200, f"{app.title}: missing {path}"
                    if path.endswith(".js"):
                        refs = re.findall(r'from\s+"([./][^\"]+)"', response.text)
                    elif path.endswith(".css"):
                        refs = re.findall(r'url\(["\']?([^"\')]+)', response.text)
                    else:
                        refs = []
                    pending.extend(urljoin(path, ref) for ref in refs)
                assert "/shared/markdown.js" in seen
                assert any(path.endswith(".woff2") for path in seen)
                assert client.get("/api/sessions").status_code == 200
                print(f"{app.title}: installed assets and session API passed")
    print(f"Installed LingChat {version('lingchat')} / LingCore {version('lingcore')}")


if __name__ == "__main__":
    main()
