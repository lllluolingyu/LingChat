"""Executable resolution and Linux process-group ownership."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path


def resolve_executable(name: str, workspace: str | Path) -> str:
    root = Path(workspace).resolve()
    # Resolve relative PATH entries as the child would, not relative to the GUI.
    for directory in os.get_exec_path():
        folder = Path(directory or ".")
        candidate = (folder if folder.is_absolute() else root / folder) / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        resolved = candidate.resolve()
        if candidate.absolute().is_relative_to(root) or resolved.is_relative_to(root):
            raise ValueError(
                f"refusing {name} executable inside workspace: {candidate}"
            )
        return str(resolved)
    raise FileNotFoundError(f"{name} is not installed on PATH")


def kill_group(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


async def close_process(proc: asyncio.subprocess.Process) -> None:
    kill_group(proc.pid)
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except TimeoutError:
        kill_group(proc.pid, signal.SIGKILL)
        await proc.wait()
    finally:
        # Descendants may outlive a CLI that has already exited.
        kill_group(proc.pid, signal.SIGKILL)


async def probe(name: str, args: list[str], workspace: Path) -> tuple[bool, str]:
    try:
        exe = resolve_executable(name, workspace)
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            return proc.returncode == 0, out.decode(errors="replace").strip()[:2000]
        finally:
            await close_process(proc)
    except (OSError, ValueError, TimeoutError) as exc:
        return False, str(exc) or "command timed out"


def has_bwrap() -> bool:
    return shutil.which("bwrap") is not None
