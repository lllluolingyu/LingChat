from pathlib import Path

import pytest

from agentgui.backends.lingcore_backend import LingCoreBackend
from agentgui.catalog import Catalog, ModelEntry
from agentgui.server import create_app
from tests.agentgui_fakes.llm import FakeLLM


@pytest.fixture
def app_factory(tmp_path: Path):
    apps = []

    def make(turns=None, tools=None, autonomy="ask"):
        directory = tmp_path / str(len(apps))
        directory.mkdir()
        workspace = directory / "workspace"
        workspace.mkdir()
        profile = directory / "config.yaml"
        profile.write_text(
            f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\ntools: {tools or []}\ntool_options:\n  run_shell:\n    require_confirmation: true\n"
        )
        fake = FakeLLM(turns or [{"text": "hello"}])
        catalog = Catalog(
            [
                ModelEntry(
                    "fake",
                    "Fake LingCore",
                    "lingcore",
                    options={"profile": str(profile)},
                )
            ]
        )
        app = create_app(
            catalog=catalog,
            store_path=directory / "gui.db",
            auth_token="secret",
            backend_factory=lambda rec, store, profiles: LingCoreBackend(
                store, profiles, lambda: fake
            ),
        )
        rec = app.state.store.create(catalog.entries["fake"], str(workspace), autonomy)
        apps.append(app)
        return app, rec, fake

    yield make
    for app in apps:
        app.state.store.close()
