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
- `caption: STRING`
- `format: enum(png,jpeg)`
- `quality: INT`
- `protect_content: BOOLEAN`
- `disable_notification: BOOLEAN`
- `skip_duplicate: BOOLEAN`
- `wait_for_upload: BOOLEAN`

Output:
- `IMAGE`

The node should remain composable in a workflow.

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
