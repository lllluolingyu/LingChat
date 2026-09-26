# Changelog

Notable user-facing LingChat changes are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Migrated the multi-backend Agent-Chat-GUI implementation into this checkout
  as the additive `agentgui` package and CLI. The existing `lingchat` command
  remains available during evaluation.
- Arbitrary image, PDF, text, and binary browser attachments, with limits
  sourced from LingCore and support for up to eight files per message.
- Per-connection shell token-prefix allowlisting from eligible confirmation
  prompts, without persisting or sharing approvals between browser sessions.
- Authenticated, forced-download access to stored attachment bytes.

### Changed

- Compatibility now covers `lingcore>=0.2.0,<0.4.0`, including the upcoming
  LingCore 0.3 line.
- Claude Code support is available through the optional `lingchat[claude]`
  extra; LingCore and Codex remain available from the base installation.
- CI tests against LingCore `main` instead of the 0.2.0 release commit, and now
  enforces Ruff lint/format plus package-wide mypy checks.
- Transcript responses omit non-image base64 payloads and expose bounded size
  and download metadata instead.
- Startup, confirmation, and security documentation now identify the selected
  host, Bubblewrap, or OCI shell runner.

### Fixed

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
