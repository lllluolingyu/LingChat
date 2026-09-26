from pathlib import Path

import pytest

from agentgui.backends._procs import resolve_executable


def test_workspace_executables_are_refused(tmp_path: Path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ValueError):
        resolve_executable("claude", tmp_path)
