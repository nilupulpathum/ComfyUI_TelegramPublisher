# Technical Specification

## 1. Telegram API

Primary methods:
- `getMe` for token/account validation
- `sendPhoto` for a single image
- `sendMediaGroup` for albums
- `sendMessage` for text
- Optional later: `getUpdates` or webhook architecture for bot commands

Use multipart/form-data for new image uploads.

## 2. Telegram client interface

Conceptual interface:

```python
class TelegramClient:
    def validate_token(self) -> BotInfo: ...
    def send_photo(self, chat_id, image_bytes, filename, caption=None, options=None): ...
    def send_media_group(self, chat_id, media_items, caption=None, options=None): ...
    def send_message(self, chat_id, text, options=None): ...
```

The concrete HTTP implementation must:
- set connect/read timeouts;
- parse Telegram `{ok, result, description, error_code, parameters}` responses;
- convert API errors into typed internal exceptions;
- never include the token in exception messages.

## 3. Image encoding

Input is a ComfyUI IMAGE tensor.

Pipeline:
1. validate tensor shape;
2. convert to CPU;
3. convert normalized float values to 8-bit;
4. create PIL image;
5. encode according to requested format;
6. return bytes plus content metadata.

Do not mutate the original tensor.

## 4. Caption templating

Supported initial variables:

```text
{{prompt}}
{{negative_prompt}}
{{seed}}
{{steps}}
{{cfg}}
{{sampler}}
{{scheduler}}
{{model}}
{{width}}
{{height}}
{{filename}}
{{timestamp}}
```

Unknown variables should resolve to an empty value and optionally produce a warning.

## 5. Duplicate detection

Default hash:
- SHA-256 of the exact encoded payload for exact-upload duplicate detection.

Optional future mode:
- perceptual hash for visually similar images.

Store:
```text
hash
encoding
chat_id
message_id
created_at
```

A duplicate decision must be scoped to destination unless explicitly configured globally.

## 6. Retry policy

Retry only transient failures:
- network timeout
- connection reset
- HTTP 429
- Telegram 5xx

Do not automatically retry:
- invalid token
- invalid chat
- bot not permitted to post
- invalid image
- malformed request

Use exponential backoff with jitter and a bounded attempt count.

## 7. SQLite schema

### accounts
```sql
id TEXT PRIMARY KEY
name TEXT NOT NULL
token_ref TEXT NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

### destinations
```sql
id TEXT PRIMARY KEY
name TEXT NOT NULL
account_id TEXT NOT NULL
chat_id TEXT NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

### publish_jobs
```sql
id TEXT PRIMARY KEY
destination_id TEXT NOT NULL
status TEXT NOT NULL
image_hash TEXT
filename TEXT
caption TEXT
attempts INTEGER NOT NULL DEFAULT 0
telegram_message_id TEXT
error_code TEXT
error_message TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

### generations
```sql
id TEXT PRIMARY KEY
job_id TEXT
prompt TEXT
negative_prompt TEXT
model TEXT
seed TEXT
steps TEXT
cfg TEXT
sampler TEXT
scheduler TEXT
width INTEGER
height INTEGER
created_at TEXT NOT NULL
```

## 8. Node contract

### Telegram Send Image

Inputs:
- `image: IMAGE`
- `account: configured account selector`
- `destination: configured destination selector`
- `caption: STRING` (caption template; see below)
- `format: enum(png,jpeg)`
- `quality: INT`
- `protect_content: BOOLEAN`
- `disable_notification: BOOLEAN`
- `skip_duplicate: BOOLEAN`
- `wait_for_upload: BOOLEAN`

Optional metadata inputs (all `STRING`, default `""`; used for caption
templates and history; `prompt`/`negative_prompt` use a multiline widget):
- `prompt`, `negative_prompt`, `seed`, `steps`, `cfg`, `sampler`,
  `scheduler`, `model`

Output:
- `IMAGE`

The node should remain composable in a workflow.

Side effects:
- `caption` is rendered with `services.captions.render_caption` against
  the metadata variables (section 4 plus `width`/`height`/`filename` from
  the encoded frame). Unknown placeholders resolve to `""` with a logged
  warning (fail-safe); the send proceeds.
- The encoded payload's SHA-256 is computed. When `skip_duplicate` is
  true, `services.dedup.DuplicateDetector` refuses the publish on a
  destination-scoped success-history hit; a duplicate-lookup DB failure
  warns and proceeds (fail-open).
- History is lazy and best-effort (`history/publisher.sqlite3`): one
  `publish_jobs` row (`success` with the Telegram message id, or `failed`
  with `error_code`/`error_message`) plus one `generations` row. Any
  history failure warns and never blocks publishing.

### Telegram Send Album

Inputs:
- `images: IMAGE` (batch; must contain 2..10 frames — Telegram
  `sendMediaGroup` limits. Any other count is a `ValueError`; single
  frames belong to Telegram Send Image)
- `account: configured account selector`
- `destination: configured destination selector`
- `caption: STRING` (caption template, rendered as for Send Image; the
  rendered text is attached to the first album item only)
- `format: enum(png,jpeg)`
- `quality: INT`
- `protect_content: BOOLEAN` (accepted for contract parity; currently not
  forwarded by the `sendMediaGroup` wrapper)
- `disable_notification: BOOLEAN` (accepted for contract parity; currently
  not forwarded by the `sendMediaGroup` wrapper)
- `skip_duplicate: BOOLEAN` (checks EVERY frame hash; the first hit
  refuses the WHOLE album — no partial album is ever sent)
- `wait_for_upload: BOOLEAN` (accepted; publishing is synchronous)

Optional metadata inputs: same eight `STRING` inputs as Send Image
(`prompt`, `negative_prompt`, `seed`, `steps`, `cfg`, `sampler`,
`scheduler`, `model`).

Output:
- `IMAGE` (the input batch, unchanged)

History: one success `publish_jobs` row (`image_hash` = first-frame hash,
`telegram_message_id` = comma-joined message ids, `filename`/`caption`
from the first frame/rendered text) plus one `generations` row; failures
record a `failed` row best-effort, then raise. History failures never
block publishing.

## 9. Configuration

Use a local configuration file or OS-appropriate secret store abstraction. Do not put secrets in `NODE_CLASS_MAPPINGS`, serialized widget state, workflow templates, or README examples.

For MVP, a local secret file with restrictive permissions is acceptable; implement a provider interface so OS keychain support can be added later.

## 10. Error model

```text
TelegramPublisherError
├── ConfigurationError
├── AuthenticationError
├── DestinationError
├── EncodingError
├── DuplicateError
├── RateLimitError
├── TransientTelegramError
└── PermanentTelegramError
```

Errors shown to users should explain the action required.

## 11. Logging

Allowed:
- account name
- destination name
- job ID
- Telegram error code
- elapsed time
- image dimensions

Never log:
- bot token
- Authorization headers
- complete multipart payload
- private message content unless explicitly enabled for debugging
