# ComfyUI Telegram Publisher

A production-minded ComfyUI custom-node extension for publishing generated images and generation metadata to Telegram.

## Project goal

Build a secure, reliable Telegram publishing layer for ComfyUI that goes beyond basic `sendPhoto` nodes:

- Secure bot/account configuration outside workflow JSON
- Image and batch/album publishing
- Automatic generation metadata extraction
- Caption templates
- Background upload queue
- Retry and failure handling
- Duplicate detection
- Local SQLite history
- Telegram destinations/profiles
- Optional Telegram-to-ComfyUI remote control in a later phase
- Optional review/approval workflow in a later phase

## Vibe-coding rule

AI coding agents must treat the documents in `docs/` as the source of truth. Do not invent requirements that conflict with them.

Start with:
1. `docs/PRD.md`
2. `docs/ARCHITECTURE.md`
3. `docs/TECHNICAL_SPEC.md`
4. `tasks/BACKLOG.md`
5. `docs/AI_CODING_GUIDE.md`

## Development principle

Build in vertical slices. Every task should end with:
- implementation
- tests
- documentation if behavior changed
- a runnable/manual verification step
- no unrelated refactors

## Initial supported target

- Windows + ComfyUI Portable
- Python backend
- ComfyUI custom node architecture
- Telegram Bot API
- SQLite for local state
- No external server required for MVP

## Non-goals for MVP

- Cloud-hosted service
- Multi-user SaaS
- Telegram Mini App
- LLM-based caption generation
- Remote workflow execution
- Automatic publishing without explicit user configuration

## Installation

Windows + ComfyUI Portable focused guide: **[docs/INSTALL.md](docs/INSTALL.md)**.

It covers prerequisites (Python `>=3.10`, ComfyUI Portable), installing the
extension into `ComfyUI\custom_nodes`, creating a bot via BotFather
(placeholders only, e.g. `<PASTE_TOKEN_HERE>`), finding your chat/channel ID,
the planned local-config approach (settings UI pending T050), the sample
workflow (pending T010), troubleshooting (extension not appearing, Python
version, Pillow missing), and token safety (never paste tokens into workflow
JSON, issues, or logs).
