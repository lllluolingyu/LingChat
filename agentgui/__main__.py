"""agentgui command line entrypoint."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .server import create_app

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentgui",
        description="One local browser UI for Claude Code, Codex and LingCore",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--config", type=str, default=None, help="models.toml path")
    args = parser.parse_args(argv)
    if args.host not in _LOOPBACK and not args.allow_remote:
        print(
            f"error: refusing non-loopback host {args.host!r}; pass --allow-remote explicitly",
            file=sys.stderr,
        )
        return 2
    app = create_app(config=args.config)
    host = (
        f"[{args.host}]"
        if ":" in args.host and not args.host.startswith("[")
        else args.host
    )
    print(
        f"Agent-Chat-GUI: open http://{host}:{args.port}/?token={app.state.auth_token}"
    )
    print("Keep this URL private; the token grants access to local agents.")
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=64 * 1024 * 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
