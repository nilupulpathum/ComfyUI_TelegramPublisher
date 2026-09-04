# Test Plan

## Unit tests

### Telegram client
- successful `getMe`
- successful `sendPhoto`
- successful `sendMediaGroup`
- malformed JSON
- Telegram `ok=false`
- HTTP 400
- HTTP 401
- HTTP 403
- HTTP 429 with retry-after
- HTTP 500
- timeout

### Encoder
- RGB image
- RGBA PNG
- JPEG conversion
- quality bounds
- invalid tensor shape
- empty batch

### Captions
- all supported variables
- unknown variable
- multiline caption
- Telegram caption length handling

### Duplicate detector
- same payload
- different payload
- same image to different destination
- disabled mode

### Storage
- create schema
- insert job
- update job
- failed job
- successful job
- migration/version behavior

## Integration tests

Use a fake Telegram HTTP server.

Verify:
```text
ComfyUI node
 -> encoder
 -> publisher
 -> fake Telegram API
 -> stored job result
```

## Manual acceptance

1. Install in clean ComfyUI.
2. Configure bot.
3. Configure destination.
4. Run sample workflow.
5. Confirm image arrives.
6. Confirm workflow continues.
7. Stop network and verify failure.
8. Restore network and verify retry.
9. Verify token is absent from workflow JSON.
10. Verify token is absent from logs.
