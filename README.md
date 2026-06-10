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
uv run lingchat                           # serves the repo's coding profile by default
# then open http://127.0.0.1:8000
```

Flags: `-p/--profile <path>`, `-w/--workspace <dir>`, `--host`, `--port`.
Without `-w` the agent works in the profile's own `workspace/` directory
(auto-created) — pass `-w /path/to/project` to point it at real files.

## Sessions

Conversations persist through lingcore's session store (`sessions.db` in the
profile directory — see LingCore invariant 14). The sidebar lists stored
sessions; **＋ New chat** starts a fresh one, clicking a session switches to it
(the transcript is replayed from storage), and **×** deletes it. The current
session id lives in the URL hash and is sent as `?session=<id>` on every
(re)connect, so a page reload — or the client's auto-reconnect after a server
restart — resumes the same conversation instead of silently starting a fresh
agent. A session already open in another tab is refused with `session_busy`.

The bundled profiles live at the LingCore repo root (`profiles/`), outside the
installed package, so they keep history out of the box. When a profile *can't*
persist — it sits inside an installed package, or sets
`sessions.enabled: false` — the sidebar stays visible and shows why instead of
listing sessions (the reason also appears in the server log and as `notice` in
`GET /api/sessions`).

REST, for the sidebar: `GET /api/sessions` (list; carries `notice` when
persistence is off), `GET /api/sessions/{id}` (transcript),
`PATCH /api/sessions/{id}` (`{"title": ...}` rename),
`DELETE /api/sessions/{id}` (409 while attached to a live socket).

## ⚠️ Security

The agent can run shell commands, so **the server is bound to `127.0.0.1` by
default**. Exposing the port to a network is remote code execution. If you must,
pair it with a profile whose shell is sandboxed — and even then, treat it as
trusted-local only.

## Protocol

Connect with `ws://host/ws?session=<id>` to resume a stored session (omit for a
fresh one; the `hello` reply carries the authoritative id).
Client → server: `{type:"user", text}` and `{type:"confirm_response", approved}`.
Server → client: `hello` (incl. `session`, `title`), `session_busy`, `text`,
`tool_call`, `tool_result`, `skill`, `confirm`, `final`, `error`, `turn_end`
(see `lingchat/server.py:_event_to_msg`).

## Test

```bash
cd LingChat && uv run pytest -q
```
