"""``python -m lingchat -p <profile>`` — serve a LingCore agent over the browser.

Composition root for the web frontend: parse args, build the FastAPI app for
the chosen profile, and launch uvicorn. Binds to 127.0.0.1 by default — the
agent can run shell commands, so exposing this port is remote code execution.
Pair it with a sandbox-isolated profile when you need containment.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from lingchat.server import create_app

# The LingCore repo's bundled profiles live at its root, outside the package
# tree, so sessions persist for them by default.
_DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "profiles" / "coding"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lingchat",
        description="Serve a LingCore agent over a browser chat UI.",
    )
    parser.add_argument(
        "--profile", "-p",
        default=str(_DEFAULT_PROFILE),
        help="Path to an agent profile YAML (default: built-in coding profile).",
    )
    parser.add_argument(
        "--workspace", "-w", default=None,
        help="Override the profile's workspace directory.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Host to bind (default 127.0.0.1; do NOT expose — the agent runs shell).",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default 8000).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    app = create_app(args.profile, workspace=args.workspace)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} exposes an agent that can run shell "
            "commands — this is remote code execution. Use a sandboxed profile.",
        )
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=64 * 1024 * 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
