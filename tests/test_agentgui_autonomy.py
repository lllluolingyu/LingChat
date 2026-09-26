"""Every autonomy level must be mapped by every backend.

The mappings are three independent lookups in three adapters, so a level added
to ``AUTONOMY_LEVELS`` without a home in all of them fails here rather than at
the user's first turn -- where Claude raises ``KeyError`` and the other two
silently fall through to their permissive branch instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgui.backends.claude_backend import _PERMISSION_MODES
from agentgui.backends.codex_backend import CodexBackend
from agentgui.catalog import ModelEntry
from agentgui.store import AUTONOMY_LEVELS, Store

# The ceiling each level buys, per backend. Read as: what happens *without* the
# user being asked.
EXPECTED = {
    "ask": {"claude": "default", "sandbox": "read-only"},
    "edit": {"claude": "acceptEdits", "sandbox": "workspace-write"},
}


def test_the_table_covers_exactly_the_published_levels():
    assert set(EXPECTED) == AUTONOMY_LEVELS


@pytest.mark.parametrize("autonomy", sorted(AUTONOMY_LEVELS))
def test_every_backend_maps_every_level(autonomy: str, tmp_path: Path):
    expected = EXPECTED[autonomy]
    assert _PERMISSION_MODES[autonomy] == expected["claude"]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    try:
        backend = CodexBackend(store, ["unused"])
        # thread_options() is pure, so no subprocess is needed to read the mapping.
        backend.session = store.create(
            ModelEntry("codex", "Codex", "codex"), str(workspace), autonomy
        )
        options = backend.thread_options()
    finally:
        store.close()
    assert options["sandbox"] == expected["sandbox"]
    # Constant by design: `edit` writes the workspace freely but still asks to
    # leave it, which is what Claude's acceptEdits already did.
    assert options["approvalPolicy"] == "on-request"
