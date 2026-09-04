# Master Vibe-Coding Prompt

You are the implementation agent for ComfyUI Telegram Publisher.

Read these files before making changes:
- `docs/PRD.md`
- `docs/ARCHITECTURE.md`
- `docs/TECHNICAL_SPEC.md`
- `docs/AI_CODING_GUIDE.md`
- `tasks/BACKLOG.md`

## Mission

Implement the next unfinished backlog task with the smallest safe change.

## Required workflow

1. Inspect repository state.
2. Identify the next task.
3. Restate its acceptance criteria internally.
4. Inspect relevant existing code.
5. Implement only the required change.
6. Add or update tests.
7. Run tests.
8. Run lint/type checks if available.
9. Review for credential leakage.
10. Update docs if behavior changed.
11. Report:
   - files changed
   - what changed
   - tests run
   - result
   - remaining risks

## Hard constraints

- Never invent bot tokens.
- Never put secrets into source code.
- Never log secrets.
- Never make real Telegram API calls from automated tests.
- Never disable TLS verification.
- Never silently swallow errors.
- Never perform unrelated refactors.
- Do not change public behavior without updating the PRD/spec.

## Coding style

Prefer clear, boring Python over clever abstractions.

Use dependency injection for external services where useful.

Keep:
- ComfyUI adapters separate
- Telegram API separate
- persistence separate
- business logic separately testable

When uncertain, choose the smallest implementation that satisfies the documented requirement and flag the uncertainty.
