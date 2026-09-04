# Security Plan

## Threat model

Assets:
- Telegram bot tokens
- destination identifiers
- generated images
- prompts and generation metadata
- local publishing history

Threats:
- secrets embedded in workflows
- secrets leaked through logs
- malicious workflow JSON
- accidental publication to wrong destination
- compromised local configuration
- network interception

## Controls

1. Store token outside workflow JSON.
2. Mask token fields in UI.
3. Never log tokens.
4. Use HTTPS only.
5. Use explicit destination selection.
6. Add a "test connection" action.
7. Require confirmation for destructive/future remote-control features.
8. Keep remote control disabled by default.
9. Document Telegram bot permissions.
10. Add a secret scanning check to CI.

## Remote control (bot commands: /status, /queue, /approve, /reject, /run)

Remote control is **disabled by default** and **off unless explicitly
configured**: review mode defaults to `false`, the trigger list defaults
to empty (`/run` then replies "no triggers configured" and runs
nothing), and an empty/missing admin allowlist is fail-closed (every
update is ignored silently, so scanners get no oracle).

- **Admin allowlist, fail-closed.** Only chats in
  `settings.admin_chat_ids` are ever answered. Enforcement happens in
  the receiver (`poll_once`) AND inside each mutating handler
  (`approve`/`reject`/`run` verify `cmd.chat_id` against the allowlist
  and return "not authorized" otherwise), so a handler can never be
  tricked by calling it directly.
- **Review flow (default-off gate for publishing).** With
  `review_mode: true`, nodes send NO media: the first-frame PNG is
  staged under `history/review/<jobid>.png`, a `pending_review` job row
  is recorded, and each admin gets a text notification naming the job
  id. `/approve <jobid>` sends the staged bytes once; `/reject
  <jobid>` drops them. Job ids are validated to `[A-Za-z0-9_-]{1,64}`
  before any filesystem use (path traversal guard); review files live
  ONLY under the review directory.
- **Trigger guardrails.** `/run <name>` only fires a trigger named in
  `settings.triggers`. The prompt file must exist, be a regular file,
  and be at most 5 MB (larger files are refused). The target host is
  re-validated loopback-only (`127.0.0.1`, `localhost`, `::1`) at `/run`
  time, beyond save-time validation, so triggers can never be aimed at
  remote hosts via a config file.
- **Reply hygiene.** Replies carry status/ids/truncated captions (80
  chars) only — never tokens, bytes, or tracebacks. Unexpected handler
  failures surface as the fixed string "error processing command" with
  detail in server-side logs only.
- **Bot permissions / admin requirement.** The bot needs permission to
  read messages in the admin chat (private chat with the bot, or group
  membership with privacy mode considered) and post rights in every
  destination chat/channel. Keep the admin chat private to the
  operator(s): anyone in the allowlisted chat can approve, reject, and
  trigger workflows.

## Future hardening

- OS keychain integration
- encrypted local secrets
- destination allowlist
- publish confirmation mode
- audit log
- signed configuration export
