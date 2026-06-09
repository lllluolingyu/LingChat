# LingChat

A browser front-end for [LingCore](https://github.com/Lingyu-Luo/LingCore) agents.

LingChat is a **separate front-end project**: it imports `lingcore` as a library
and bridges the agent's `AgentEvent` stream — and its shell-confirmation
round-trip — over a WebSocket. The core stays frontend-agnostic; this lives
entirely outside the `lingcore` package (LingCore invariant 3).

```
LingChat/
  lingchat/server.py    # FastAPI app + WebSocket bridge + per-connection Agent
  lingchat/__main__.py  # `python -m lingchat` / `lingchat` entry point
  web/                  # vanilla HTML/CSS/JS single page (no build step)
  tests/                # bridge tests via Starlette TestClient + a scripted fake LLM
```

## Run

```bash
cd LingChat
uv sync                                   # installs fastapi/uvicorn + editable lingcore
uv run lingchat -p ../lingcore/profiles/coding_sandboxed
# then open http://127.0.0.1:8000
```

Flags: `-p/--profile <path>`, `-w/--workspace <dir>`, `--host`, `--port`.

## ⚠️ Security

The agent can run shell commands, so **the server is bound to `127.0.0.1` by
default**. Exposing the port to a network is remote code execution. If you must,
pair it with the `coding_sandboxed` profile (bubblewrap-isolated shell) — and
even then, treat it as trusted-local only.

## Protocol

Client → server: `{type:"user", text}` and `{type:"confirm_response", approved}`.
Server → client: `hello`, `text`, `tool_call`, `tool_result`, `skill`,
`confirm`, `final`, `error`, `turn_end` (see `lingchat/server.py:_event_to_msg`).

## Test

```bash
cd LingChat && uv run pytest -q
```
