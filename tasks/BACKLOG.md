# Product Backlog

Priority: P0 highest.

## Epic 1 — Foundation (done)

- [x] T001 Scaffold project using current ComfyUI extension template
- [x] T002 Configure package metadata and node registration
- [x] T003 Add test framework and CI
- [x] T004 Create Telegram error model
- [x] T005 Implement Telegram HTTP client
- [x] T006 Implement token validation
- [x] T007 Implement local account configuration
- [x] T008 Implement image encoder
- [x] T009 Implement Send Image node
- [x] T010 Add sample workflow
- [x] T011 Write installation documentation

## Epic 2 — Reliability (done)

- [x] T020 Add typed retry policy
- [x] T021 Add 429 handling
- [x] T022 Add timeout handling
- [x] T023 Add structured logging with secret redaction
- [x] T024 Add integration tests using fake Telegram server

## Epic 3 — Publishing (done)

- [x] T030 Implement batch extraction
- [x] T031 Implement Send Album node
- [x] T032 Add metadata extractor
- [x] T033 Add caption template engine
- [x] T034 Add duplicate detector
- [x] T035 Add SQLite history
- [x] T036 Persist Telegram message IDs

## Epic 4 — Queue (done)

- [x] T040 Create persistent publish job model
- [x] T041 Create bounded worker queue
- [x] T042 Add retry scheduling
- [x] T043 Add queue status
- [x] T044 Ensure graceful shutdown

## Epic 5 — UX (done)

- [x] T050 Settings UI
- [x] T051 Account selector
- [x] T052 Destination selector
- [x] T053 Connection test
- [x] T054 Publish status indicator
- [x] T055 User-friendly errors

## Epic 6 — Remote features (done)

- [x] T060 Telegram command receiver
- [x] T061 `/status`
- [x] T062 `/queue`
- [x] T063 review mode
- [x] T064 approve/reject
- [x] T065 remote workflow trigger

## Epic 7 — Bot command driver (open)

The Epic 6 receiver is host-driven (`poll_once`) with no autostart by
design. Nothing in the extension calls it yet, so bot commands do not
respond in a live install. Epic 7 wires a driver without violating the
security stance (explicit user action, fail-closed allowlist, no
background threads at import).

- [x] T070 Telegram Command Poller node — an explicit output node that
  long-polls `getUpdates` while its prompt runs (account + poll count /
  timeout inputs), dispatches through the Epic 6 router, and returns
  without autostarting anything. Must work with ComfyUI execution
  (no blocking beyond its own run) and degrade to a clear message
  when no admin chats are configured.
- [x] T071 Expire abandoned review payloads — TTL/GC for `pending_review`
  rows and their PNG files (Epic 6 risk: they accumulate until
  approved/rejected). Rejected-by-expiry must preserve the job record.
- [x] T072 Forward album flags — plumb `protect_content` /
  `disable_notification` through `send_media_group` (currently accepted
  by the node but ignored by the client wrapper).

## Vibe-coding execution order

Epics 1–6 complete. Suggested order for Epic 7:

T070 -> live end-to-end verification (real ComfyUI + bot + channel)
-> T071 -> T072

Do not widen Epic 6 remote-control powers (new commands, broader
triggers) before T070 is live and the allowlist behavior is verified
against a real bot.
