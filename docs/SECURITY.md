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

## Future hardening

- OS keychain integration
- encrypted local secrets
- destination allowlist
- publish confirmation mode
- audit log
- signed configuration export
