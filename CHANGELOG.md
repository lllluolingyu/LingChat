# Changelog

Notable user-facing LingChat changes are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- An interface language for the AgentGUI browser UI, in Chinese and English,
  switched from a toggle beside the theme one and remembered per browser
  (`agentgui/web/js/i18n.js`). Static markup carries `data-i18n` attributes and
  the rest calls `t()`; `<html lang>` is applied before first paint because it
  selects the CJK face the font stacks fall through to. Changing language
  repaints the open page, transcript included. The legacy `lingchat` UI is
  unchanged and stays English.
- A per-backend mark (`agentgui/web/js/marks.js`): GPT-series models — every
  Codex entry, and any LingCore profile named for one — get a six-lobe blossom
  instead of the radiating asterisk, on the reply avatar, the empty state, the
  sidebar badge and beside the model picker. The glyph is built from three
  rotated stadium loops rather than traced, so it stays legible at the 15px the
  avatar renders.
- A single `usage` frame shape across all three backends (`agentgui/usage.py`),
  carrying per-model token counts plus `scope`/`cumulative` so a consumer can
  price spend without guessing each agent's counter semantics. Codex now also
  reports its served model and cache-write/reasoning tokens, Claude reports
  subagent-inclusive per-model totals, and LingCore reports provider usage from
  its new `UsageReported` event (including requests billed before a Stop).
- Migrated the multi-backend Agent-Chat-GUI implementation into this checkout
  as the additive `agentgui` package and CLI. The existing `lingchat` command
  remains available during evaluation.
- Arbitrary image, PDF, text, and binary browser attachments, with limits
  sourced from LingCore and support for up to eight files per message.
- Per-connection shell token-prefix allowlisting from eligible confirmation
  prompts, without persisting or sharing approvals between browser sessions.
- Authenticated, forced-download access to stored attachment bytes.

### Changed

- **AgentGUI now opens in Chinese.** English remains one click away in the
  sidebar foot, and the choice persists per browser. Only the interface is
  translated: model labels, backend names, workspace paths and agent output are
  left exactly as their source gives them.
- Both browser UIs are restyled onto a warm paper-and-clay palette with a
  typographic split — Inter for the interface, Source Serif 4 for agent replies,
  JetBrains Mono for code. The three variable fonts are self-hosted under
  `web/fonts/` (SIL OFL 1.1, Latin subsets; other scripts fall through to system
  faces), so the UI still needs no network access and no build step.
- Page backgrounds are flat colour, and the panels no longer glow: gradient
  washes, coloured bloom shadows, inset sheens and hover lifts are gone, and
  depth is stated with a hairline plus one of two short shadows. Backdrop blur
  is kept only where something actually passes behind a panel — the approval
  card, the scroll-to-latest button and a consumer-injected overlay — since over
  a flat background a blurred translucent fill resolves to the same solid colour
  at the cost of a compositing pass.
- The whole stylesheet is now ordered by one emphasis ladder, documented at the
  top of the file: **main content ≈ input box > code / notes > top bar > tool
  calls > background**. In practice the top bar became a flush hairlined strip
  instead of a floating card, tool calls became hairline rows that only take a
  fill once opened, and code steps *away* from the page in whichever direction
  the page itself goes — dark on the dark canvas, light grey on the ivory one.
- Design tokens changed with it. Added `--serif`, `--surface-input`,
  `--surface-overlay`, `--code-edge` and `--bubble-user`; removed
  `--accent-glow`, `--brand-grad`, `--user-grad`, `--glass-highlight`,
  `--glass-border`, `--glass-border-strong` (use `--border`/`--border-strong`),
  `--glass-shadow-soft`, `--glass-blur-strong`, `--shadow-1`, `--shadow-2`,
  `--surface` and `--surface-floating`. Both themes are still driven entirely
  from these, so a consumer that only references tokens picks the new look up
  untouched.
- Requires `lingcore>=0.3.0,<0.4.0`: usage reporting depends on LingCore
  0.3's `UsageReported` event, `Agent.drain_usage()`, and `lingcore.usage`.
- Claude Code support is available through the optional `lingchat[claude]`
  extra; LingCore and Codex remain available from the base installation.
- CI tests against LingCore `main` instead of the 0.2.0 release commit, and now
  enforces Ruff lint/format plus package-wide mypy checks.
- Transcript responses omit non-image base64 payloads and expose bounded size
  and download metadata instead.
- Startup, confirmation, and security documentation now identify the selected
  host, Bubblewrap, or OCI shell runner.

### Fixed

- AgentGUI's Claude sessions raise an approval for WebSearch, WebFetch, writes
  and commands again. The custom transport never passed
  `--permission-prompt-tool stdio`, so the CLI denied them itself without asking.
- The model picker's option list is legible under the dark theme; options had
  inherited a near-transparent fill and rendered pale behind light text.
- Browser connections deep-copy nested tool options so a session allowlist can
  never leak into another connection through the shared profile.

## [0.2.0] - 2026-07-20

### Added

- Stop support with explicit cancellation lifecycle frames, rejection of
  concurrent submissions, and immediate reuse after a cancelled turn.
- Editing and regeneration from stored user messages while retaining original
  attachments, plus atomic conversation-prefix forks and branch provenance.
- Replay of LingCore's durable compaction and dynamic-skill events, including
  monotonic event cursors for incremental consumers.
- Stable message sequence identifiers in transcript responses and browser
  controls for editing or forking valid branch boundaries.

### Changed

- Requires LingCore 0.2.x and uses its schema-v2 session, cancellation, replay,
  fork, and original-input APIs.
- Updated the pinned web stack to FastAPI 0.139.2 and Uvicorn 0.51.0.
- Session switching, reconnect, and transcript rendering now preserve derived
  runtime events alongside canonical messages.

### Fixed

- Outbound WebSocket failures now close an abandoned Agent stream
  deterministically instead of attempting cancellation finalization from the
  still-running driver task.
- An immediate Stop that lands before `Agent.run()` acquires its checkpoint is
  treated as a successful cancellation instead of raising an internal error.
- Disconnect cleanup cancels and awaits the active turn before releasing the
  per-process session attachment lease.
- Confirmation responses now fail closed: only literal JSON `true` approves a
  gated command, and malformed protocol messages cannot crash the reader loop.
- Switching sessions while the old WebSocket is still connecting now closes
  that socket instead of leaving a stale connection holding the prior session.
- Invalid profile/configuration startup errors now return exit status 2 with a
  concise diagnostic instead of a traceback.

### Compatibility

- Requires Python 3.11 or newer and `lingcore>=0.2.0,<0.3.0`.
- The CLI still requires an explicit profile. LingCore wheels do not contain
  the repository's writable example profiles; point `--profile` at an external
  profile directory or a LingCore source checkout.
- Disconnecting cancels the in-flight turn. Completed-state replay is durable,
  but detached execution across browser disconnects is not implemented.

## [0.1.0] - 2026-06-09

- Initial browser frontend with authenticated WebSocket streaming, tool
  confirmation, multimodal attachments, and profile-scoped session history.

[0.2.0]: https://github.com/lllluolingyu/LingChat/releases/tag/v0.2.0
