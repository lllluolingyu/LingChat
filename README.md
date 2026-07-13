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
uv run lingchat -p ../profiles/daily      # serve a profile (choose it explicitly)
# then open the printed http://127.0.0.1:8000/?token=... URL
```

`-p/--profile` is **required**: the profile decides what the served agent can
do — serving `../profiles/coding` means serving an agent with shell access, so
that is an explicit choice rather than a default. Other flags:
`-w/--workspace <dir>`, `--host`, `--port`, `--allow-remote`.
Without `-w` the agent works in the profile's own `workspace/` directory
(auto-created) — pass `-w /path/to/project` to point it at real files.
IPv6 literals are passed raw to `--host` (for example `::1`); the printed
browser URL adds the required brackets automatically.

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

The agent may run shell commands, so **the server binds to `127.0.0.1` and
refuses any other host unless `--allow-remote` is given**. Exposing the port to
a network is remote code execution for a shell-enabled profile. If you must,
put TLS and network controls in front, prefer a profile without `run_shell`,
and even then treat it as trusted-local only.

Every protected `/api` request and WebSocket handshake is authenticated with
the per-launch token printed at startup (`?token=...` on the initial URL and
WebSocket, `X-LingChat-Token` on REST calls). The page stores the token in
`sessionStorage` and immediately scrubs it from the address bar, so reloads keep
working without the token lingering in history. WebSocket handshakes from a
browser must also match the server's full origin (scheme and authority).

## Protocol

Connect with `ws://host/ws?token=<launch-token>&session=<id>` to resume a stored
session (omit `session` for a fresh one; the `hello` reply carries the
authoritative id). REST calls send the same token in `X-LingChat-Token`.
Client → server: `{type:"user", text}` and
`{type:"confirm_response", id, approved}`; the `id` echoes the corresponding
server `confirm` message so parallel confirmations cannot be crossed.
Server → client: `hello` (incl. `session`, `title`), `session_busy`, `text`,
`tool_call`, `tool_result`, `skill`, `compact`, `stream_retry`, `confirm`,
`final`, `error`, `turn_end` (see `lingchat/server.py:_event_to_msg`).

## Test

```bash
cd LingChat && uv run pytest -q
```

The current suite contains 24 tests.
