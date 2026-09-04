# Architecture Decision Records

## ADR-001: Direct Telegram Bot API

**Decision:** Use Telegram Bot API over HTTPS directly.

**Reason:** Small surface area, fewer dependencies, easier testing, and direct control over retries/errors.

## ADR-002: SQLite for local history

**Decision:** SQLite.

**Reason:** No external database required and adequate for a local ComfyUI extension.

## ADR-003: Secrets outside workflow JSON

**Decision:** Store credentials in local configuration/secret storage.

**Reason:** Workflow files are commonly shared/exported and must remain safe.

## ADR-004: Return IMAGE from publisher node

**Decision:** Publisher nodes return the original IMAGE.

**Reason:** Publishing is a side effect and should not unnecessarily break downstream image processing.

## ADR-005: Build correctness before background execution

**Decision:** Implement reliable synchronous publishing first, then background queue.

**Reason:** Background concurrency introduces lifecycle, shutdown, retry, and execution-order complexity.
