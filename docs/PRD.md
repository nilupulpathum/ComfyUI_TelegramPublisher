# Product Requirements Document

## 1. Product

**Name:** ComfyUI Telegram Publisher  
**Working package name:** `ComfyUI-TelegramPublisher`

## 2. Problem

Existing Telegram ComfyUI nodes can solve the basic case of sending an image to a chat/channel. The project aims to provide a reliable publishing workflow with secure configuration, batch handling, metadata, retry behavior, duplicate prevention, and a local generation history.

## 3. Target user

A ComfyUI user who generates images locally and wants selected generations automatically or manually published to Telegram channels/groups/chats.

## 4. Product vision

Turn Telegram into a reliable publishing endpoint for ComfyUI without requiring a separate cloud backend.

## 5. MVP features

### P0
- Telegram bot account configuration
- Destination/chat ID configuration
- Send single image
- Return original IMAGE unchanged so the node can be inserted into a workflow
- JPEG/PNG output encoding
- Caption input
- Telegram API error reporting
- Secure token storage outside workflow JSON
- Config validation
- Unit tests for Telegram client and image encoding

### P1
- Batch/album sending
- Caption templates
- Metadata extraction
- Background upload worker
- Retry with exponential backoff
- SQLite generation history
- SHA-256 duplicate detection
- Upload status UI/logging

### P2
- Multiple accounts
- Multiple named destinations
- Profile presets
- Protected content / silent publishing controls
- Telegram message IDs stored in history
- Publish statistics

### P3
- Telegram bot commands
- Remote status
- Review/approve/reject workflow
- Remote workflow triggering

## 6. User stories

1. As a ComfyUI user, I can select a configured Telegram account and destination without putting the bot token into the workflow.
2. As a user, I can connect an IMAGE output to a Telegram Publisher node and receive the same IMAGE output downstream.
3. As a user, I can send a batch as a Telegram album.
4. As a user, I can create a caption using metadata placeholders.
5. As a user, I can retry a failed upload without regenerating the image.
6. As a user, I can prevent identical images from being published twice.
7. As a user, I can inspect local publishing history.
8. As a user, I can see actionable errors when Telegram rejects a request.

## 7. Functional requirements

FR-001 Account credentials must not be persisted inside workflow JSON.
FR-002 The node must support Telegram chat IDs as strings so numeric IDs and usernames can be represented.
FR-003 The publisher must preserve the input IMAGE as its output.
FR-004 A failed Telegram upload must produce an actionable error and must not corrupt the image tensor.
FR-005 Batch images must be handled deterministically.
FR-006 Duplicate detection must be optional.
FR-007 Every publish attempt must have a local status.
FR-008 Retry must not create uncontrolled duplicate messages.
FR-009 Caption templates must fail safely when a placeholder is unavailable.
FR-010 The extension must work without an external cloud service.

## 8. Non-functional requirements

- Security: never log full bot tokens.
- Reliability: transient HTTP failures should be retried.
- Performance: image conversion should avoid unnecessary copies.
- Maintainability: Telegram API code must be isolated from ComfyUI node classes.
- Testability: core services must be unit-testable without starting ComfyUI.
- Compatibility: avoid assumptions about GPU vendor or model type.
- Privacy: metadata sending must be opt-in.

## 9. Success criteria

MVP is successful when a fresh ComfyUI installation can:
1. install the extension;
2. configure a bot securely;
3. load a workflow;
4. generate an image;
5. publish it to a configured Telegram destination;
6. continue the workflow through the node;
7. report failures clearly;
8. run automated unit tests successfully.

## 10. Risks

- Telegram rate limits
- Telegram file-size/media constraints
- ComfyUI execution/caching semantics
- Workflow JSON accidentally containing secrets
- Blocking generation execution during network uploads
- Changes in ComfyUI frontend APIs

## 11. Product decisions

- Prefer official Telegram Bot API directly over a large Telegram SDK.
- Keep the core backend dependency-light.
- SQLite is sufficient for local history.
- Use the current official ComfyUI extension/scaffold conventions rather than copying legacy node layouts.
