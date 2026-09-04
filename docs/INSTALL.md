# Installation — ComfyUI Telegram Publisher

Windows + ComfyUI Portable focused guide.

> Scope note: Epic 1 (Foundation) is implemented — scaffold, registration,
> Telegram client (T004–T006), local configuration (T007), image encoder
> (T008), Send Image node (T009), sample workflow (T010), the COMBO
> selectors (T051/T052), the node buttons + HTTP API (T050/T053/T054), and
> this guide (T011) all exist. Anything below that does not exist
> yet is explicitly marked with its backlog ID from `tasks/BACKLOG.md`.

## 1. Prerequisites

- Windows 10/11 (64-bit).
- ComfyUI Portable installed, unzipped, and confirmed to start at least once
  (so its `python_embeded` folder and `ComfyUI\custom_nodes` folder exist).
- Python `>=3.10` — required by `pyproject.toml` (`requires-python = ">=3.10"`).
  ComfyUI Portable ships its own embedded Python; you do not need a separate
  system Python as long as the embedded one satisfies `>=3.10`.
- Network access to `api.telegram.org` (HTTPS) from the machine running
  ComfyUI (`docs/SECURITY.md`: HTTPS only).
- A Telegram account (to talk to BotFather and to add the bot to your
  chat/channel).

Check the embedded Python version before installing:

```powershell
<ComfyUI_Portable_Dir>\python_embeded\python.exe --version
```

It must report Python 3.10 or newer. If it is older, update your
ComfyUI Portable build first.

## 2. Install the extension

1. Locate your ComfyUI Portable directory, e.g. `<ComfyUI_Portable_Dir>`.
   The custom-nodes folder is:

   ```text
   <ComfyUI_Portable_Dir>\ComfyUI\custom_nodes
   ```

2. Copy or clone this repository into that folder so the entry point lands at:

   ```text
   <ComfyUI_Portable_Dir>\ComfyUI\custom_nodes\ComfyUI-TelegramPublisher\__init__.py
   ```

   Clone example (folder name matters — keep `__init__.py` at its top level):

   ```powershell
   cd <ComfyUI_Portable_Dir>\ComfyUI\custom_nodes
   git clone <REPO_URL> ComfyUI-TelegramPublisher
   ```

   (`<REPO_URL>` is the URL of this repository. The `ComfyUI-TelegramPublisher`
   folder must contain `__init__.py`, `pyproject.toml`, and `requirements.txt`
   directly — all three exist in the repo root today.)

3. Install the runtime dependency (Pillow — declared in both
   `requirements.txt` and `pyproject.toml`) using the **embedded** Python:

   ```powershell
   cd <ComfyUI_Portable_Dir>\ComfyUI\custom_nodes\ComfyUI-TelegramPublisher
   <ComfyUI_Portable_Dir>\python_embeded\python.exe -m pip install -r requirements.txt
   ```

   Equivalent alternative:

   ```powershell
   <ComfyUI_Portable_Dir>\python_embeded\python.exe -m pip install "Pillow>=10"
   ```

4. Restart ComfyUI Portable completely (close the window / stop the server,
   then start it again) so it re-imports custom nodes.

5. Verify: ComfyUI should start with no import errors mentioning
   `ComfyUI-TelegramPublisher` or `__init__.py`. The extension registers
   the **Telegram Send Image** node (T009) and the **Telegram Send Album**
   node (T031) under the Telegram category.
   So "installed correctly" means ComfyUI starts cleanly AND a
   "Telegram Send Image" node and a "Telegram Send Album" node appear in
   the Add Node menu.

## 3. Create a bot via BotFather

No real tokens are used in this guide — everywhere a token goes, the
placeholder `<PASTE_TOKEN_HERE>` is used.

1. In Telegram, open a chat with `@BotFather`.
2. Send `/newbot` and follow the prompts (display name + username ending in
   `bot`).
3. BotFather replies with a bot token. Copy it into a safe place (a password
   manager). In any example below it appears only as `<PASTE_TOKEN_HERE>`.
4. Optional but recommended: send `/setprivacy` to BotFather if you add the
   bot to groups, and review its admin rights before adding it to a channel
   (see `docs/SECURITY.md` control 9 — document bot permissions).
5. Do **not** paste the token into any workflow JSON, chat message, issue, or
   log (see section 8).

## 4. Find your chat / channel ID

The publisher identifies destinations with string chat IDs
(`docs/PRD.md` FR-002: chat IDs are strings so numeric IDs and usernames both
work). Use placeholders such as `<PASTE_CHAT_ID_HERE>` — never real IDs in
shared files.

Options:

- **Private chat with your bot:** message your bot first (bots cannot message
  you first), then open `https://api.telegram.org/bot<PASTE_TOKEN_HERE>/getUpdates`
  in a browser and look for `"chat":{"id": ...}`. That number is your chat ID.
- **Group:** add the bot to the group, send a message mentioning it, then use
  the same `getUpdates` call and read the group chat `id` (usually negative).
- **Channel:** add the bot as an administrator with post rights, post a
  message, and resolve it via `getUpdates` or a forwarding helper bot. A
  public channel can also be addressed by its `@username` string.
- **Helper bots** (e.g. ID-display bots) also work: forward any message from
  the target chat to the helper and read the ID it reports.

If you use `getUpdates`, replace `<PASTE_TOKEN_HERE>` in the URL with your
real token **only in your own browser address bar** — never save that URL in
a file.

## 5. Configure the extension

Planned approach (per `docs/PRD.md` FR-001 and `docs/SECURITY.md`):
credentials are stored in **local configuration outside workflow JSON**, and
the bot token is never persisted inside a workflow file.

- Local account/destination configuration itself is coming in **T007**
  (secure local configuration). The exact file location and format are still
  to be defined by that task — do not create your own config file yet; any
  path you may see discussed elsewhere is not final until T007 lands.
- The node `account`/`destination` inputs are COMBO dropdowns (T051/T052):
  options are read from the on-disk config each time the node is created,
  with `""` first (explicit unset — publishing with `""` fails with an
  actionable error telling you to pick a configured id). If you edit the
  config file while ComfyUI is running, either press the node's
  **Refresh Telegram lists** button (reloads the dropdowns in the browser)
  or restart ComfyUI to pick the change up.
- Each Telegram node carries two buttons (T050, `web/settings.js`): **Test
  Telegram connection** (POSTs the currently selected account/destination
  to `/telegram_publisher/test_connection` and `alert()`s the result) and
  **Refresh Telegram lists** (re-fetches the dropdown options). Until T007
  lands, keep your token in your password manager and use
  `<PASTE_TOKEN_HERE>` as the placeholder in any notes or drafts.

## 6. Run the sample workflow

The minimal sample workflow `workflows/basic_send_image.json` ships with
the extension (T010). It wires an `EmptyImage` node (no model/checkpoint
needed) into the **Telegram Send Image** node (T009) with placeholder
account/destination ids (`my-account`, `my-channel`) — replace those with
your own configured ids before running; never put tokens in the file.

- The other examples listed in `workflows/README.md` now also ship:
  `send_album.json` (EmptyImage batch of 2 into **Telegram Send Album**,
  T031) and `metadata_caption.json` (caption template `{{prompt}}` with
  the optional metadata inputs filled in). Both use placeholder ids
  (`my-account`, `my-channel`) — replace those with your own configured
  ids before running; never put tokens in the files.

Publish history is stored in a local SQLite file at
`<extension>/history/publisher.sqlite3` (created automatically on first
publish). Both nodes also accept optional metadata inputs (`prompt`,
`negative_prompt`, `seed`, `steps`, `cfg`, `sampler`, `scheduler`,
`model`) used for `{{placeholder}}` caption templates and history rows.

Both nodes have a `wait_for_upload` flag (`BOOLEAN`, default `True`).
With the default, the node uploads before returning. With
`wait_for_upload=False` (background mode), the node validates,
encodes, and enqueues the publish, then returns immediately while a
shared background worker uploads later; the persisted `publish_jobs`
row in `history/publisher.sqlite3` moves from `queued` to `success`
(with the Telegram message id) or `failed` (with `error_code` /
`error_message`). Until the status UI lands (T054), job outcomes for
background publishes are visible in two places: the ComfyUI console
log (look for `telegram publish queued job_id=...`, then worker
success/transient/failure lines) and the `publish_jobs` table itself,
e.g. `SELECT id, status, attempts, telegram_message_id, error_code
FROM publish_jobs ORDER BY created_at DESC LIMIT 10;` with any SQLite
reader. `queued` rows with a future `next_retry_at` are scheduled
retries, not stuck jobs.

The expected flow (per `docs/PRD.md` section 9) is: load the sample
workflow → generate an image → publish it to the configured Telegram
destination → continue the workflow through the node → check for clear
failure messages.

## 7. Troubleshooting

- **Extension does not appear / no Telegram nodes in the Add Node menu.**
  The **Telegram Send Image** node (T009) and **Telegram Send Album**
  node (T031) should appear under the Telegram
  category. If they are missing, or ComfyUI itself fails to start after
  copying the folder, check:
  1. The folder layout — `<...>\custom_nodes\ComfyUI-TelegramPublisher\__init__.py`
     must exist (root `__init__.py` is the ComfyUI entry point).
  2. A full restart of ComfyUI (not just a browser refresh).
  3. The console/log for tracebacks mentioning `publisher_nodes/__init__.py` or
     `NODE_CLASS_MAPPINGS` (registration errors are loud by design).
- **Wrong Python version.** `pyproject.toml` requires `>=3.10`. Re-run
  `<ComfyUI_Portable_Dir>\python_embeded\python.exe --version`. Use the
  embedded Python for both ComfyUI and `pip install` — a system Python may be
  a different version and install Pillow where ComfyUI cannot see it.
- **`Pillow` missing / `PIL` import errors.** The image encoder (T008) needs
  `Pillow>=10` (see `requirements.txt`). Re-run the pip step from section 2
  with the embedded Python and restart ComfyUI. Verify with:
  ```powershell
  <ComfyUI_Portable_Dir>\python_embeded\python.exe -c "import PIL; print(PIL.__version__)"
  ```
- **Telegram API errors.** Per `docs/PRD.md`: failed uploads produce an
  actionable error without corrupting the image tensor. For connectivity
  issues (firewall/proxy blocking `api.telegram.org`), check with a plain
  browser visit to `https://api.telegram.org`.
- **Still stuck?** Re-read `workflows/README.md` (confirms no workflow files
  exist yet), `tasks/BACKLOG.md` (confirms which IDs are still open), and the
  console output. When asking for help, include the ComfyUI console log with
  any secrets redacted (see next section).

## 8. Token safety — read this before configuring anything

Per `docs/SECURITY.md` and `docs/PRD.md` FR-001 / section 10 (risks):

- **Never** paste a bot token into a workflow `.json` file. Workflow files
  are shared, screenshotted, and committed — anything in them leaks.
- **Never** paste tokens (or private chat IDs) into GitHub issues, chat
  messages, screenshots, videos, or logs. Use `<PASTE_TOKEN_HERE>` and
  `<PASTE_CHAT_ID_HERE>`.
- **Never** share a workflow file that you suspect contains a secret without
  opening it in a text editor and searching for the token first.
- **Never** log full tokens. If you add logging or report a bug, redact the
  token (show at most the last 3–4 characters if you must identify it).
- Keep the token in a password manager or another secrets store, not in
  notes or in the ComfyUI workflow canvas.
- If a token ever leaks (committed, posted, screenshotted): talk to
  `@BotFather`, send `/revoke`, and replace the token immediately.

## 9. Enabling remote control (review mode + bot commands + triggers)

All remote control is OFF unless explicitly configured (see
`docs/SECURITY.md` for the full contract).

1. **Admin ids.** Message your bot, resolve your numeric chat id (section
   4), and put it in the config `settings` object:
   `"admin_chat_ids": ["<PASTE_CHAT_ID_HERE>"]`. Only these chats are
   ever answered; an empty list answers nobody.
2. **Review mode.** Set `"review_mode": true` to stage publishes for
   approval instead of sending immediately. Admins then release each
   image with `/approve <jobid>` or drop it with `/reject <jobid>`.
   Staged payloads live under `<extension>/history/review/`.
3. **Command list.** `/help`, `/status`, `/queue` (read-only);
   `/approve <jobid>`, `/reject <jobid>` (review); `/run <name>`
   (trigger a local workflow, see next step).
4. **Trigger setup.** Add a named trigger pointing at a ComfyUI API
   prompt file (JSON, at most 5 MB):
   `"triggers": [{"name": "portrait",
   "prompt_file": "<ABSOLUTE_PATH_TO_PROMPT_JSON>"}]`, then run it from
   chat with `/run portrait`. Triggers only ever POST to loopback
   (`comfy_host` must be `127.0.0.1`, `localhost`, or `::1`;
   `comfy_port` defaults to `8188`).

### Driving the bot: Telegram Command Poller node (T070)

The commands above only answer while the **Telegram Command Poller**
node runs — nothing polls in the background (by design, see
`docs/SECURITY.md`).

1. Set `admin_chat_ids` in the config `settings` object (step 1 above).
2. Add the **Telegram Command Poller** node (Telegram category, output
   node), pick the bot's `account`, and queue the prompt. Defaults poll
   3 rounds of 10s each (worst case ~30s of blocking inside that run).
3. DM the bot `/status` (or `/queue`, `/approve <jobid>`, ...). The node
   returns `"<n> replies sent"` (or `"no new commands"` when there is
   nothing new). With no admin chats configured it returns
   `"Telegram polling skipped: ..."` without touching the network —
   set `admin_chat_ids` and run again.

## 10. What exists vs. what is upcoming| Item | Status |
| --- | --- |
| Scaffold + registration (`__init__.py`, `publisher_nodes/__init__.py`) | Exists |
| Dependency declarations (`pyproject.toml`, `requirements.txt`, Pillow, numpy) | Exist |
| Settings placeholder (`web/settings.js`) | Exists (placeholder only) |
| Telegram client / error model | Exists (T004–T006) |
| Local account configuration | Exists (T007) |
| Image encoder (Pillow) | Exists (T008) |
| Send Image node | Exists (T009) |
| Send Album node (2–10 frame batches) | Exists (T031) |
| Caption templates + metadata inputs | Exist (Epic 3) |
| Local history (`history/publisher.sqlite3`) | Exists (Epic 3) |
| Sample workflow JSON (`workflows/*.json`) | `basic_send_image.json` (T010), `send_album.json`, `metadata_caption.json` all exist |
| Account/destination dropdowns (COMBO) | Exist (T051/T052) — `""` first = unset; restart ComfyUI or use Refresh to pick up config edits |
| Node buttons: Test connection + Refresh lists | Exist (T050/T053) — see manual browser verification below |

## 10. Manual browser verification (T050 frontend, ComfyUI frontend 1.x)

The `web/settings.js` extension uses only long-standing frontend APIs, but
frontend module paths vary by ComfyUI version, so verify by hand once:

1. Start ComfyUI, open the browser, add a **Telegram Send Image** node.
2. Confirm the `account`/`destination` widgets render as dropdowns and the
   node shows **Test Telegram connection** and **Refresh Telegram lists**
   buttons.
3. Pick an account/destination, press **Test Telegram connection**, and
   confirm the `alert()` reports OK (or an actionable error, never a token).
4. Press **Refresh Telegram lists** and confirm the dropdowns reload.
5. Open devtools → Network and confirm the calls hit
   `GET /telegram_publisher/accounts`,
   `GET /telegram_publisher/destinations`, and
   `POST /telegram_publisher/test_connection` with no token in any
   request or response body.

Source of truth for status: `tasks/BACKLOG.md` together with
`docs/PRD.md`, `docs/SECURITY.md`, and `docs/RELEASE_PLAN.md`
(v0.1.0 — Foundation).
