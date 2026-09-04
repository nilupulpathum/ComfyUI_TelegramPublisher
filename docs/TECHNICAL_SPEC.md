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
- `account: COMBO selector of configured account ids` (options read fresh
  from the on-disk config on every `INPUT_TYPES` call: `""` first =
  explicit unset, then sorted account ids; publishing with `""` raises the
  existing actionable error. Config edits made outside ComfyUI require a
  ComfyUI restart to appear in the dropdown; the `web/settings.js`
  extension additionally refreshes options live in the browser via
  `GET /telegram_publisher/accounts` without a restart)
- `destination: COMBO selector of configured destination ids` (same
  semantics as `account`: `""` first, then sorted destination ids,
  refreshed live via `GET /telegram_publisher/destinations`)
- `caption: STRING` (caption template; see below)
- `format: enum(png,jpeg)`
- `quality: INT`
- `protect_content: BOOLEAN`
- `disable_notification: BOOLEAN`
- `skip_duplicate: BOOLEAN`
- `wait_for_upload: BOOLEAN` (see "Background publishing" below)

Optional metadata inputs (all `STRING`, default `""`; used for caption
templates and history; `prompt`/`negative_prompt` use a multiline widget):
- `prompt`, `negative_prompt`, `seed`, `steps`, `cfg`, `sampler`,
  `scheduler`, `model`

Output:
- `IMAGE`

The node should remain composable in a workflow.

#### Background publishing (`wait_for_upload`, Epic 4)

Both nodes share the same two modes:

- `wait_for_upload=True` (default): synchronous. The node uploads
  before returning; history gets one `publish_jobs` row (`success`
  with the Telegram message id, or `failed` with
  `error_code`/`error_message`) plus one `generations` row.
- `wait_for_upload=False`: background. The node runs the SAME
  pre-flight as the sync path (config resolution, duplicate check when
  `skip_duplicate`, encoding, caption rendering with warnings,
  SHA-256), builds a `services.queue.PublishPayload` whose `metadata`
  dict is `GenerationMetadata.as_template_vars()` PLUS
  `image_hash` (first-frame payload hash, the row's `image_hash`
  source) and `filename` (first filename), enqueues it, logs
  `job_id`/`account`/`destination`, and returns the input IMAGE(S)
  immediately with NO network on the call. Validation, duplicate,
  caption, and enqueue errors still raise synchronously (fail fast
  before enqueue, wrapped as `RuntimeError` like the sync path, with a
  best-effort `failed` history row at `attempts=0`).

Background rows: `enqueue` persists a `queued` row first, then the
worker uploads (single attempt per try; the WORKER owns all retries --
the sender client is built with `retry_policy=None` so there is no
double-retry layering, FR-008) and moves the row to `success` (with
the sender's message-id string) or `failed` (`RetryExhausted` /
`PayloadLost` / typed error name). Transient failures requeue with
`next_retry_at = now + min(retry_after, max_delay)`; a worker that
takes a not-yet-due job puts it back untouched and sleeps until due
(capped at `poll_interval * 5`) instead of attempting early. No
`generations` row is written for background jobs.

Shared queue lifecycle: background publishes under both nodes go
through `services.queue.get_shared_queue(str(history_db_path),
factory)` -- one started process-wide queue per history database,
created once and shut down by an `atexit` backstop
(`_shutdown_shared_queues`) because ComfyUI has no extension-unload
hook. Queue state is readable at any time via
`PublishQueue.status()` (`queued`/`sending`/`success`/`failed` row
counts, `worker_running`, `buffered`, `maxsize`); the node-level
status indicator UI is Epic 5 T054.

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
- `account: COMBO selector of configured account ids` (`""` first =
  explicit unset, then sorted ids; fresh read per `INPUT_TYPES` call, so a
  ComfyUI restart picks up out-of-band config edits; live browser refresh
  via `GET /telegram_publisher/accounts`)
- `destination: COMBO selector of configured destination ids` (same
  semantics; live browser refresh via
  `GET /telegram_publisher/destinations`)
- `caption: STRING` (caption template, rendered as for Send Image; the
  rendered text is attached to the first album item only)
- `format: enum(png,jpeg)`
- `quality: INT`
- `protect_content: BOOLEAN` (forwarded to `sendMediaGroup` as
  `"true"`/`"false"`, same encoding as Send Image)
- `disable_notification: BOOLEAN` (forwarded to `sendMediaGroup` as
  `"true"`/`"false"`, same encoding as Send Image)
- `skip_duplicate: BOOLEAN` (checks EVERY frame hash; the first hit
  refuses the WHOLE album — no partial album is ever sent)
- `wait_for_upload: BOOLEAN` (see "Background publishing" above:
  `False` enqueues an `album` payload with all files and returns
  immediately; per-send `protect_content`/`disable_notification` are
  carried on the payload and forwarded by the background sender)

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

### Telegram Command Poller

Explicit Epic 7 driver for the section 9.2 bot commands: while its prompt
runs, the node long-polls `getUpdates` and dispatches through the Epic 6
router (`register_builtins` + `register_review`), then returns a STRING
summary. No threads, no autostart, no module-level polling (the security
stance is preserved: remote control stays off unless explicitly
configured, and nothing in the extension polls without the user queueing
this node).

Inputs (no image inputs — this node drives the bot, it doesn't publish):
- `account: COMBO selector of configured account ids` (same semantics
  as the Send nodes: `""` first = explicit unset, then sorted ids, read
  fresh from the on-disk config on every `INPUT_TYPES` call; polling
  with `""` raises the existing actionable error)
- `poll_rounds: INT` (default 3, min 1, max 20) — number of `getUpdates`
  polls per run
- `poll_timeout: INT` (default 10, min 1, max 30) — seconds per
  `getUpdates` long-poll

Output:
- `STRING` summary, always a one-element tuple of `str`, never None:
  `"<n> replies sent"`, `"no new commands"`, or the skip message.

Blocking bound: worst case `poll_rounds x poll_timeout` seconds
(default 3 x 10s = 30s). Each round is one `getUpdates` long-poll whose
HTTP read timeout exceeds the poll window.

Skip semantics: with no `admin_chat_ids` configured the node logs a
warning and returns `Telegram polling skipped: no admin chats
configured...` with ZERO network calls (fail-closed degrade: no client
is built, no `getUpdates` is issued). An empty poll (no new commands)
returns `no new commands` — not an error. Client/network failures raise
`RuntimeError("Telegram polling failed: ...")` (chained, token-free,
same contract as the publishers).

Offset files: `<db parent>/offsets/<safe-account-id>.json` (next to
`history/publisher.sqlite3`), where the account id is sanitized to
`[A-Za-z0-9_-]` (other chars map to `"_"`). Sanitization collisions are
harmless: approve/reject are idempotent and an offset rewind only
reprocesses updates. The bot context passes `queue=None`, so `/status`
reports `worker=off` while polling — starting the shared worker here
would be a side effect beyond polling.

## 9. Configuration

Use a local configuration file or OS-appropriate secret store abstraction. Do not put secrets in `NODE_CLASS_MAPPINGS`, serialized widget state, workflow templates, or README examples.

For MVP, a local secret file with restrictive permissions is acceptable; implement a provider interface so OS keychain support can be added later.

### 9.1 Remote-control settings (`settings` object)

```jsonc
{
  "settings": {
    "review_mode": false,          // default OFF; true = stage for approval
    "admin_chat_ids": ["123"],     // explicit allowlist (strings, FR-002)
    "comfy_host": "127.0.0.1",     // loopback only (save-time validated)
    "comfy_port": 8188,
    "triggers": [                  // default empty = /run runs nothing
      {"name": "portrait", "prompt_file": "workflows/portrait_api.json"},
      {"name": "anima",             // T080/T081: canvas + override + publish
       "prompt_file": "<ABSOLUTE_PATH_TO_CANVAS_JSON>",
       "prompt_targets": [{"node": "28", "input": "text"}],
       "prompt_required": false,
       "publish": {"account": "my-account", "destination": "my-channel",
                   "caption_template": "{{prompt}}",
                   "format": "png", "quality": 90}}
    ]
  }
}
```

### 9.2 Bot commands

| Command | Args | Effect | Reply |
| --- | --- | --- | --- |
| `/help` | — | list commands | command list |
| `/status` | — | queue counts, worker state, most recent job | status block |
| `/queue` | — | queued/sending jobs (caption preview 80 chars) | listing or "queue is empty" |
| `/approve` | `<jobid>` | send staged payload once, mark `success` | `published to <dest>, message <id>` |
| `/reject` | `<jobid>` | drop staged payload, mark `failed`/`Rejected` | `rejected publish to <dest>` |
| `/run` | `<name> [text]` | POST prompt file to local ComfyUI `/prompt` (canvas auto-converted, text substituted into `prompt_targets`, publish node auto-appended) | `workflow started: <prompt_id>` |

Unknown commands and non-command text get NO reply. Non-allowlisted
chats get NO reply (warning log with chat id only). Handler failures
return the fixed string `error processing command`.

### 9.3 Review lifecycle (T063/T064)

1. Node (both Send Image and Send Album) runs the normal pre-flight
   (config resolution, duplicate check, encoding, caption rendering,
   hashing). When `settings.review_mode` is true, it then stages
   instead of sending: `history/review/<jobid>.png` holds the
   first-frame PNG (job id = uuid4 hex), a `publish_jobs` row with
   status `pending_review` (`attempts=0`) plus a `generations` row are
   recorded best-effort, each admin id gets a `send_message`
   notification (`New Telegram publish pending review from <dest>:
   <caption80> — reply /approve <jobid> or /reject <jobid>`), and the
   input IMAGE is returned with NO media sent. Review takes precedence
   over `wait_for_upload=False` (ignored with an info log).
2. `/approve <jobid>`: unknown/malformed id -> `unknown review job
   '<arg>'`; non-pending row -> `already handled (status <s>)`;
   missing payload -> row marked `failed`/`PayloadLost` +
   `review payload missing; job marked failed`; otherwise the staged
   bytes are sent via `sendPhoto` (filename = row filename or
   `review_<id>.png`), the row moves to `success` with the message id,
   the file is discarded, and the handler replies
   `published to <dest>, message <id>`.
3. `/reject <jobid>`: same lookup; a pending row moves to
   `failed`/`Rejected`, the file is discarded (idempotent), reply
   `rejected publish to <dest>`.

### 9.3.1 Review expiry (T071)

Abandoned `pending_review` rows/payloads (never approved/rejected) are
reclaimed by `services.review.prune_expired` with
`DEFAULT_REVIEW_TTL_S = 604800` (7 days). Per `*.png` file under the
review directory (job id = file stem, re-validated against
`[A-Za-z0-9_-]{1,64}`; anything else is a stray file: kept + warning):

- no job row -> file deleted (`"expired"`);
- row terminal (`success`/`failed`) with a leftover file -> file deleted,
  record preserved (`"orphans"`);
- row `pending_review` older than the TTL (`now - created_at > ttl_s`,
  `created_at` = UTC ISO row timestamp) -> row marked `failed`
  (`error_code="Expired"`, `error_message="review expired without
  approval"`; the record is preserved, only the PNG is removed) +
  file deleted (`"expired"`);
- row `pending_review` within TTL -> kept (`"kept"`);
- row `pending_review` with missing/unparseable `created_at`, or a row
  with any other non-terminal status -> kept + warning (fail-safe
  toward PRESERVATION).

`prune_expired(review_dir, jobs, *, ttl_s=DEFAULT_REVIEW_TTL_S,
now=None) -> {"expired", "orphans", "kept"}` never raises on per-file
errors (lookup/update/delete failures warn and count the file as
`"kept"`); `now` is epoch seconds (defaults to wall clock) for
deterministic tests. GC host: the Telegram Command Poller node calls it
best-effort once per `poll()` run with the default TTL before the first
`getUpdates` poll; any GC failure warns and polling continues.

### 9.4 Trigger contract (T065, extended T080/T081)

`/run <name> [text...]` resolves `<name>` against `settings.triggers`
(no match -> `unknown trigger '<name>'`, empty list ->
`unknown trigger '<name>' (no triggers configured)`). Free text is
`" ".join(args[1:])`, capped at 1500 chars (`prompt too long ...`
otherwise). The prompt file must exist, be a regular file, and be at
most 5 MB (`trigger misconfigured: ...` otherwise); it must hold JSON.

#### 9.4.1 Prompt file formats (T080)

`services.canvas.detect_format` classifies the parsed file:

- `canvas`: dict with list `nodes` + list `links` (frontend save
  format) — converted at run time (see 9.4.2);
- `api_wrapped`: single top key `prompt` holding the node map — the
  inner map is used (one `prompt` level is unwrapped);
- `api_bare`: non-empty dict of `{id: {class_type, inputs}}` — used
  as-is (same bytes as the T065 path).

Anything else is `trigger misconfigured: ...`.

#### 9.4.2 Canvas conversion (T080)

Canvas files convert via `services.canvas.canvas_to_prompt` using the
target server's own `/object_info` (read-only GET to the same
loopback-validated `http://<host>:<port>`, timeout 30 s; transport
failures -> `trigger failed: ...`, malformed/conversion failures ->
`trigger misconfigured: ...`). `object_info` is taken as data
(`{"Type": {"input": {"required": {name: [typedef, opts]}}}}`).

Conversion rules:

- Keep nodes with `mode` in `(0, None/missing)`; DROP `mode != 0`,
  types `{"Note", "Reroute"}`, and types starting with `"Label"`.
  Entries missing `id`/`type` -> `ValueError` listing them.
- Links resolve via the links table
  (`[id, from_node, from_slot, to_node, to_slot, type]`). A kept input
  whose link id is missing or points at a dropped/missing node ->
  `ValueError` naming node+input (fail loudly, never silently rewire).
- Per kept node (`str(id)` keys): linked inputs become
  `[str(from_node), from_slot]`; unlinked inputs WITH a `"widget"`
  marker consume `widgets_values` positionally, SKIPPING top-level
  dict values (custom-node UI state such as LoraManager autocomplete
  metadata — never backend inputs), falling back to the spec required
  default when values run out; unlinked inputs WITHOUT a marker take
  the spec required default; no value and no default -> `ValueError`
  naming node+input. Unknown node types (absent from `object_info`)
  -> `ValueError` naming the type.
- Spec defaults: `required[name] = [typedef, opts]`; the default is
  `opts.get("default")` when `opts` is a dict. Optional inputs are
  included ONLY when linked (frontend parity).
- Known approximation: `widgets_values` mapping is positional, so
  value-only widgets without a canvas input entry (e.g. KSampler's
  `control_after_generate`) shift the values that follow. The live
  Anima trial (T082) must confirm real workflows convert faithfully.

#### 9.4.3 Prompt text override (T081)

Each trigger may declare `prompt_targets: [{"node": "<id>",
"input": "<name>"}]` (default `input` is `"text"`) plus
`prompt_required: bool` (default `false`):

- text given + targets: every target is set to the text. Target node
  missing, input missing, or the current value not a string (linked
  inputs arrive as `[id, slot]` lists) -> `trigger misconfigured: ...`
  (`... is not overridable (linked?)` for the non-string case).
- text given + NO targets: refused loudly
  (`usage: /run <name> [text] (this trigger takes no prompt text)` —
  user text is never silently dropped).
- no text + targets + `prompt_required`: `usage: /run <name> <text>`;
  no text + not required: file text is kept.

#### 9.4.4 Publish auto-append (T080)

A trigger `publish` block
(`{account, destination, source?, caption_template?,
format?, quality?}`; defaults `caption_template="{{prompt}}"`,
`format="png"`, `quality=90`) appends a `Telegram Send Image` node via
`services.canvas.append_publish` after the override step:

- Source: explicit `source: "id:slot"` (node must exist in the map) or
  auto-detect — the first `VAEDecode` node in map order supplies slot
  0 (the API map carries no output metadata, so the slot is a
  documented convention). No VAEDecode / bad source -> `trigger
  misconfigured: ...`.
- The new id is `str(max(int ids) + 1)` (`"telegram_send_1"` when no
  key parses as an int). The input map is copied, never mutated.
- Node inputs: `image [src_id, 0]`, `account`, `destination`,
  `caption` (already rendered — see below), `format`, `quality`,
  `protect_content False`, `disable_notification False`,
  `skip_duplicate False`, `wait_for_upload True`.
- Caption: `render_caption(caption_template, {"prompt": <override
  text, else the first target's file text, else "">})`. Render
  warnings are dropped deliberately (`_run` has no logger; truncation
  is fail-safe and still applies).

The converted map (with overrides + appended node) is posted as
`{"prompt": <map>}` via stdlib `urllib` to
`http://<comfy_host>:<comfy_port>/prompt` (timeout 30 s, loopback
re-validated at call time). The response must hold `prompt_id`
(`workflow started: <prompt_id>`); every transport/parse failure maps
to `trigger failed: <actionable>` with no tokens or tracebacks.

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
