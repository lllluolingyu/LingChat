# LingChat

A browser front-end for [LingCore](https://github.com/lllluolingyu/LingCore) agents.

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

## Install and run

LingChat 0.2 requires Python 3.11 or newer and LingCore 0.2.x. A packaged
installation resolves the compatible core automatically:

```bash
pip install lingchat
lingchat --profile /path/to/profile
```

For source development, the uv configuration intentionally uses a sibling
editable LingCore checkout. Clone LingChat inside it:

```bash
git clone https://github.com/lllluolingyu/LingCore.git
git clone https://github.com/lllluolingyu/LingChat.git LingCore/LingChat
cd LingCore/LingChat
uv sync
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
agent. Compaction snapshots and dynamic skill state live in the same database:
the rebuilt agent starts from the newest valid compacted working set plus its
later transcript tail, and restores active skills that the current profile still
permits. Compaction and skill-transition notes are replayed into the browser at
their original message positions. A session already open in another tab is
refused with `session_busy`.

While a response is running, **Stop** cancels the model/tool task, removes its
partial assistant/tool transcript, and keeps the submitted user message. A
second submission is rejected instead of queued. Hover a stored user message
and choose **Edit** to delete that message and the later branch, then regenerate
from the edited text; any attachments on the original message are retained.
Choose **Fork** on a stored user message to copy the prefix into a new session
and regenerate that message there, leaving the original branch intact. Fork on
a final assistant reply opens a new continuation from that answer; intermediate
tool-call messages are not offered because they are not complete branch
boundaries. The new session records its immediate parent, root session, and
source message sequence.
Conversation rollback cannot undo an external side effect from a tool that
finished before Stop. Stop/Edit also remove compacted/skill events belonging to
the discarded branch, while event cursor ids are never reused.

Fork is conversation-only: both branches use the same profile workspace and
see the same files/tool side effects. Stored attachment payloads are copied into
the fork's SQLite rows; workspace files are not snapshotted.

This replay is completed-state replay, not detached execution: disconnecting
still cancels and repairs an in-flight model/tool turn. Keeping a task alive
without a browser is a separate durable-runner lifecycle.

LingCore's example profiles live at its repository root (`profiles/`), outside
the installed package, so they keep history out of the box. When a profile *can't*
persist — it sits inside an installed package, or sets
`sessions.enabled: false` — the sidebar stays visible and shows why instead of
listing sessions (the reason also appears in the server log and as `notice` in
`GET /api/sessions`).

REST, for the sidebar and replay consumers: `GET /api/sessions` (list; carries
`notice` when persistence is off), `GET /api/sessions/{id}` (transcript plus
message-anchored `events` and `event_cursor`),
`GET /api/sessions/{id}/events?after=<cursor>` (incremental compaction/skill
events with a monotonic next `cursor`),
`POST /api/sessions/{id}/fork` (`{"through_seq": 12, "title": "optional"}`;
omit `through_seq` to copy the full transcript),
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
authoritative id and current `event_cursor`). REST calls send the same token in
`X-LingChat-Token`.
Client → server: `{type:"user", text, attachments?}`, `{type:"stop"}`,
`{type:"edit", seq, text}`, and `{type:"confirm_response", id, approved}`.
The edit `seq` is the stable sequence returned on each message by
`GET /api/sessions/{id}`; the confirmation `id` echoes the corresponding server
`confirm` message so parallel confirmations cannot be crossed. Only the literal
JSON boolean `true` approves a confirmation; malformed or missing values deny.

Server → client: `hello` (incl. `session`, `title`, `event_cursor`), `session_busy`,
`turn_busy`, `text`, `tool_call`, `tool_result`, `skill`, `compact`,
`stream_retry`, `confirm`, `cancelled`, `stop_ignored`, `edit_accepted`,
`edit_rejected`, `final`, `error`, and `turn_end` (see
`lingchat/server.py:_event_to_msg`). A successful Stop or Edit regeneration ends
with `turn_end`; a rejected edit and an ignored stop are standalone replies.

## Test

```bash
cd LingChat && uv run pytest -q
```
