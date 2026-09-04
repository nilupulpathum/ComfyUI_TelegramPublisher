# Product Backlog

Priority: P0 highest.

## Epic 1 — Foundation

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

## Epic 2 — Reliability

- [x] T020 Add typed retry policy
- [x] T021 Add 429 handling
- [x] T022 Add timeout handling
- [x] T023 Add structured logging with secret redaction
- [x] T024 Add integration tests using fake Telegram server

## Epic 3 — Publishing

- [x] T030 Implement batch extraction
- [x] T031 Implement Send Album node
- [x] T032 Add metadata extractor
- [x] T033 Add caption template engine
- [x] T034 Add duplicate detector
- [x] T035 Add SQLite history
- [x] T036 Persist Telegram message IDs

## Epic 4 — Queue

- [ ] T040 Create persistent publish job model
- [ ] T041 Create bounded worker queue
- [ ] T042 Add retry scheduling
- [ ] T043 Add queue status
- [ ] T044 Ensure graceful shutdown

## Epic 5 — UX

- [ ] T050 Settings UI
- [ ] T051 Account selector
- [ ] T052 Destination selector
- [ ] T053 Connection test
- [ ] T054 Publish status indicator
- [ ] T055 User-friendly errors

## Epic 6 — Remote features

- [ ] T060 Telegram command receiver
- [ ] T061 `/status`
- [ ] T062 `/queue`
- [ ] T063 review mode
- [ ] T064 approve/reject
- [ ] T065 remote workflow trigger

## Vibe-coding execution order

Complete one vertical slice at a time:

Slice A:
T001 -> T005 -> T006 -> T008 -> T009 -> tests

Slice B:
T007 -> T009 -> T011 -> security verification

Slice C:
T020 -> T021 -> T022 -> T023 -> T024

Slice D:
T030 -> T031 -> T032 -> T033

Slice E:
T034 -> T035 -> T036

Slice F:
T040 -> T041 -> T042 -> T043 -> T044

Do not start Epic 6 before Epics 1–5 are stable.
