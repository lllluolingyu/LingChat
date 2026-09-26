"""Bounded, authenticated installation checks. Never inspect CLI credentials."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from lingcore.config import AgentProfile, ConfigError

from .backends._jsonrpc import JsonRpc
from .backends._procs import has_bwrap, probe, resolve_executable
from .catalog import Catalog, ModelEntry

CODEX_SCHEMA_VERSION = "0.156.1"


async def live_models() -> tuple[list[ModelEntry], str | None]:
    async def deny(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise ValueError(f"unexpected discovery request: {method}")

    rpc = JsonRpc(deny)
    try:
        async with asyncio.timeout(12):
            exe = resolve_executable("codex", Path.cwd())
            await rpc.start(
                [exe, "app-server", "--listen", "stdio://"], str(Path.cwd())
            )
            await rpc.request(
                "initialize",
                {"clientInfo": {"name": "agentgui-models", "version": "0.1.0"}},
            )
            await rpc.send({"method": "initialized", "params": {}})
            entries = []
            cursor = None
            for _ in range(20):
                result = await rpc.request(
                    "model/list",
                    {"cursor": cursor, "limit": 100, "includeHidden": False},
                )
                for item in result.get("data", []):
                    native = item["id"]
                    entries.append(
                        ModelEntry(
                            "codex:" + native,
                            item.get("displayName", native),
                            "codex",
                            native,
                            {"live": True},
                        )
                    )
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            return entries, None
    except Exception as exc:
        return (
            [],
            f"Codex model discovery unavailable: {str(exc) or 'timed out'}. Catalog entries remain available.",
        )
    finally:
        await rpc.close()


async def doctor(catalog: Catalog) -> dict[str, Any]:
    values = await asyncio.gather(
        probe("claude", ["--version"], Path.cwd()),
        probe("codex", ["--version"], Path.cwd()),
        probe("codex", ["login", "status"], Path.cwd()),
    )
    checks = [
        {"name": name, "ok": result[0], "detail": result[1]}
        for name, result in zip(
            ["Claude Code", "Codex", "Codex login"], values, strict=True
        )
    ]
    installed = values[1][1].split()[-1] if values[1][0] else "unknown"
    checks.append(
        {
            "name": "Codex protocol version",
            "ok": installed == CODEX_SCHEMA_VERSION,
            "detail": f"Schema: {CODEX_SCHEMA_VERSION}; installed: {installed}",
        }
    )
    bwrap = has_bwrap()
    checks.append(
        {
            "name": "Bubblewrap",
            "ok": bwrap,
            "detail": "bwrap available"
            if bwrap
            else "bwrap not found; profiles using Bubblewrap need it installed",
        }
    )
    for path in dict.fromkeys(
        e.options["profile"]
        for e in catalog.entries.values()
        if e.backend == "lingcore"
    ):
        try:
            profile = AgentProfile.load(path)
        except Exception:
            # A validation exception can embed expanded .env values. Do not echo it.
            checks.append(
                {
                    "name": f"Profile: {path}",
                    "ok": False,
                    "detail": "Profile cannot be loaded; check its config and environment locally.",
                }
            )
            continue
        env = profile.llm.api_key_env
        try:
            profile.llm.resolve_api_key()
            has_key = True
        except ConfigError:
            has_key = False
        key = (
            "not required"
            if not env
            else f"{env} configured"
            if has_key
            else f"{env} is not set"
        )
        checks.append(
            {
                "name": f"Profile: {path}",
                "ok": has_key,
                "detail": f"{profile.name} · {profile.llm.model} · {len(profile.tools)} tools · provider key {key}",
            }
        )
    return {"checks": checks}
