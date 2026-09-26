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
  web/fonts/            # self-hosted variable fonts (SIL OFL 1.1, Latin subsets)
  tests/                # bridge tests via Starlette TestClient + a scripted fake LLM
```

Both UIs share one design system: a warm paper-and-clay palette on flat
backgrounds, with dark and light themes driven entirely from tokens on `:root`
and `html[data-theme="light"]`. Interface text is set in Inter, agent replies in
Source Serif 4, and code in JetBrains Mono; the three variable fonts are served
from `web/fonts/` (and `agentgui/web/fonts/`), so nothing is fetched from a font
CDN at runtime. Scripts outside the Latin subsets — CJK in particular — fall
through to the system faces named in `--sans`, `--serif`, and `--mono`.

Emphasis is deliberate and ordered, loudest first: **main content ≈ input box >
code / notes > top bar > tool calls > background**. Depth is a hairline and one
of two short shadows, never a glow, and translucent blurred panels are reserved
for the few surfaces that genuinely float over the transcript. The ladder is
documented at the top of `agentgui/web/style.css`; read it before giving a
component a fill, a border or a shadow it does not already have.

## Multi-backend AgentGUI

This repository also contains `agentgui`, the next local GUI for Claude Code,
Codex, and LingCore. It keeps one backend selected per conversation and uses
the same approval, transcript, attachment, fork, and tool-card surface for all
three backends. The existing `lingchat` command and package remain available
while this migration is being evaluated.

Run the new GUI from this checkout with:

```bash
uv sync
uv run agentgui
```

The command prints a tokenized loopback URL. The AgentGUI catalog is loaded from
`~/.config/agentgui/models.toml` and creates a default catalog on first run.
Codex uses its installed CLI login and LingCore entries use the profiles named
in the catalog. Claude Code support is optional; install `lingchat[claude]` to
enable it. The multi-backend implementation lives under `agentgui/`, its
browser assets under `agentgui/web/`, and its adapter tests under
`tests/test_agentgui_*.py` and `tests/agentgui_fakes/`.

### Usage reporting

Every backend emits one `usage` frame shape. The flat `input`/`output`/`cached`/
`cost_usd`/`context_pct` fields drive the browser chip; `models` carries
per-model token counts for a consumer that prices spend itself. `scope` says
what the flat counters cover (`request`, `turn`, or `conversation`) and
`cumulative` says whether `models` counters are session running totals —
Claude and Codex report totals a biller must diff, LingCore reports one
request. Per-model counts follow each provider's own convention: `cached`/
`cache_write` are inside `input` for Codex and separate from it for Claude,
while `reasoning` is inside `output`. `cost_usd` is the agent's own estimate
and is never authoritative for billing.

## Install and run

This checkout requires Python 3.11 or newer and supports LingCore 0.2.x and
0.3.x (`lingcore>=0.2.0,<0.4.0`). A packaged installation resolves the
compatible core automatically:

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

A profile-selected guardrail (`guardrail.policy`, including its constructor
options) applies to LingChat exactly as it does to LingCore's terminal frontend.

## Attachments

The browser accepts any file, up to 8 attachments and 20 MiB decoded data per
message. Images are limited to 5 MiB each; PDFs, text, and binary files are
limited to 10 MiB each. LingCore inspects the bytes and authoritatively assigns
one of four kinds:

- `image` and PDF `file` attachments can reach a model as native media when its
  profile declares that modality.
- `text` attachments are copied into the workspace and their UTF-8 content is
  inlined into the prompt within LingCore's text budget.
- `binary` attachments are copied into the workspace and represented to the
  model by a pointer so workspace tools can inspect them.

Every accepted file is stored under the profile workspace's `attachments/`
directory. Stored non-image transcript payloads are downloaded through an
authenticated endpoint instead of being embedded in history responses. PDF to
text degradation for a model without native PDF support requires the optional
`lingcore[pdf]` extra.

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

LingCore checkout profiles live at its repository root (`profiles/`). Its wheel
can copy immutable templates into writable user state with
`lingcore profile init`; either form keeps history out of the box. When a
profile *can't* persist — it sits inside an installed package, or sets
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
put TLS and network controls in front and treat it as trusted-local only.

Shell-enabled profiles may select LingCore's strict, fail-closed Bubblewrap or
Docker/Podman OCI runner through `tool_options.run_shell.sandbox`; see
[LingCore's sandboxing guide](https://github.com/lllluolingyu/LingCore/blob/main/docs/sandboxing.md).
LingChat names the selected runner at startup, in every confirmation dialog,
and in the shell result header. A profile without a sandbox block uses the
`host (unsandboxed)` runner. Sandboxing bounds an approved command's OS access;
it is not a substitute for the network boundary or for user confirmation.

An eligible simple shell command can be allowed for the rest of the current
browser connection. These token-prefix allowlists are connection-local, are
never persisted, and do not cross tabs or reconnects. Commands containing shell
control syntax cannot be added to the allowlist.

Every protected `/api` request and WebSocket handshake is authenticated with
the per-launch token printed at startup (`?token=...` on the initial URL and
WebSocket, `X-LingChat-Token` on REST calls). The page stores the token in
`sessionStorage` and immediately scrubs it from the address bar, so reloads keep
working without the token lingering in history. WebSocket handshakes from a
browser must also match the server's full origin (scheme and authority).

## Protocol

Connect with `ws://host/ws?token=<launch-token>&session=<id>` to resume a stored
session (omit `session` for a fresh one; the `hello` reply carries the
authoritative id, current `event_cursor`, and a `limits` object sourced from
LingCore's attachment constants, with `max_attachments`, `image_max_bytes`,
`file_max_bytes`, and `total_max_bytes` fields). REST calls send the same token
in `X-LingChat-Token`.
Client → server: `{type:"user", text, attachments?}`, `{type:"stop"}`,
`{type:"edit", seq, text}`, and
`{type:"confirm_response", id, approved, scope?:"once"|"session"}`.
The edit `seq` is the stable sequence returned on each message by
`GET /api/sessions/{id}`; the confirmation `id` echoes the corresponding server
`confirm` message so parallel confirmations cannot be crossed. Only the literal
JSON boolean `true` approves a confirmation; malformed or missing values deny.
Confirm frames carry the shell `runner` and, only for an eligible in-flight
`run_shell` call, an `allowlist_pattern`. The server recomputes that pattern
from its stored command and ignores any client-supplied pattern.

Server → client: `hello` (incl. `session`, `title`, `event_cursor`), `session_busy`,
`turn_busy`, `text`, `tool_call`, `tool_result`, `skill`, `compact`,
`stream_retry`, `confirm`, `shell_allowlist`, `cancelled`, `stop_ignored`, `edit_accepted`,
`edit_rejected`, `final`, `error`, and `turn_end` (see
`lingchat/server.py:_event_to_msg`). A successful Stop or Edit regeneration ends
with `turn_end`; a rejected edit and an ignored stop are standalone replies.
`shell_allowlist` carries the server-derived `pattern` (or `null` when none is
safe) and an `added` boolean.

Transcript attachments carry `size` and `download`; `data` is omitted for every
kind except images, whose bytes remain inline for previews. Stored bytes are
available at
`GET /api/sessions/{id}/messages/{seq}/attachments/{index}` with the same auth
header. Downloads are always served as `application/octet-stream` with an
attachment disposition, `nosniff`, and `no-store`.

## Test

```bash
cd LingChat && uv run pytest -q
```
