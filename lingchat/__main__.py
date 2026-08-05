"""``python -m lingchat -p <profile>`` — serve a LingCore agent over the browser.

Composition root for the web frontend: parse args, build the FastAPI app for
the chosen profile, and launch uvicorn. Binds to 127.0.0.1 by default — the
agent can run shell commands, so exposing this port is remote code execution.
Prefer a profile without ``run_shell``; actual containment requires isolation
outside LingChat.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn
from lingcore.errors import LingCoreError

from lingchat.server import create_app

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lingchat",
        description="Serve a LingCore agent over a browser chat UI.",
    )
    # No default profile: serving an agent is serving whatever that agent can
    # do (a coding profile can run shell commands), so the operator names the
    # profile explicitly instead of inheriting a shell-enabled one silently.
    parser.add_argument(
        "--profile",
        "-p",
        required=True,
        help="Path to an agent profile YAML or its directory (e.g. a LingCore "
        "checkout's profiles/daily). Required: the profile decides what the "
        "served agent can do, including shell access.",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default=None,
        help="Override the profile's workspace directory.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default 127.0.0.1, loopback only). A non-loopback "
        "host is refused unless --allow-remote is also given.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow binding to a non-loopback host. Anyone who can "
        "reach the port and token gets everything the agent can do — for a "
        "shell-enabled profile that is remote code execution. Put TLS and "
        "network controls in front, and prefer a profile without run_shell.",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind (default 8000)."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.host not in _LOOPBACK_HOSTS and not args.allow_remote:
        # Refuse (don't just warn) before any state is touched: exposing the
        # port exposes the agent, and that must be an explicit operator choice.
        print(
            f"error: refusing to bind non-loopback host {args.host!r}: this "
            "exposes the agent to the network (remote code execution for a "
            "shell-enabled profile). Re-run with --allow-remote only if the "
            "network path is secured.",
            file=sys.stderr,
        )
        return 2
    try:
        app = create_app(args.profile, workspace=args.workspace)
    except LingCoreError as exc:
        print(f"error: failed to start LingChat: {exc}", file=sys.stderr)
        return 2
    token = app.state.auth_token
    if args.host not in _LOOPBACK_HOSTS:
        print(
            f"WARNING: binding to {args.host} exposes an agent that can run shell "
            "commands — this is remote code execution. Prefer a profile without "
            "run_shell; actual containment requires external isolation.",
        )
    # Print the URL carrying the per-launch auth token: the browser authenticates
    # with it, and requests without it (or from another origin) are refused.
    # RFC 3986 requires brackets around an IPv6 literal in a URL. Uvicorn takes
    # the raw ``::1`` host, but the browser-facing form is ``[::1]``.
    url_host = (
        f"[{args.host}]"
        if ":" in args.host and not args.host.startswith("[")
        else args.host
    )
    url = f"http://{url_host}:{args.port}/"
    if token:
        url = f"{url}?token={token}"
    print(f"LingChat: open {url}")
    print("  (the token authenticates the browser — keep this URL private)")
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=64 * 1024 * 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
