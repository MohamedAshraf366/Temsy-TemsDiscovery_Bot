# TEMS Discovery Import Monitor

A Windows automation tool that watches **TEMS Discovery** while it imports drive-test log files and sends rich notifications to **WhatsApp** (and optionally Telegram). It can also be controlled remotely from WhatsApp: browse the machine, pick `.trp` files, choose the target project/dataset, and start the real TEMS import dialog — then report progress automatically.

The project is designed to run unattended: a small watcher starts the monitor automatically as soon as `DiscoveryCore.exe` (TEMS Discovery) is launched, and stops it when TEMS closes.

> **Platform:** Windows 10/11 only (UI Automation, Win32 APIs).
> **Tested with:** Python 3.14, TEMS Discovery with an English UI.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [WhatsApp / GREEN-API setup](#whatsapp--green-api-setup)
- [Running the monitor](#running-the-monitor)
- [Remote control from WhatsApp](#remote-control-from-whatsapp)
- [Automatic start with TEMS](#automatic-start-with-tems)
- [Logs](#logs)
- [Troubleshooting](#troubleshooting)
- [Security and privacy](#security-and-privacy)
- [Publishing to GitHub](#publishing-to-github)
- [Disclaimer](#disclaimer)

---

## Features

- **Import monitoring** — reads the TEMS Discovery *Task Window* summary line through UI Automation (with an OCR fallback) and detects when an import starts, finishes, or fails.
- **WhatsApp notifications** — sends messages through [GREEN-API](https://green-api.com/) using the official WhatsApp HTTP API. Telegram, CallMeBot, and WAHA are also supported.
- **Automatic progress reports** — file count, project/dataset, duration, completed/failed files, and elapsed time.
- **Remote control** — send commands from WhatsApp to browse drives/folders, select files, and start the real TEMS **Import Drive Test Data** dialog.
- **Safe confirmation** — nothing is imported until you send `/yes`.
- **Smart file selection** — counts only `.trp` files in the selected folder, newest first; `/all` is capped by `max_files`, and `/all N` imports the newest *N* files.
- **Project inference** — the target project is inferred from the folder name under the configured root (e.g. `D:\WORK\Dayrout Subzone\...` → `Dayrout Subzone`).
- **Scheduled motivation messages** — optional AI-generated (OpenRouter/OpenAI) or local motivational messages every *N* hours.
- **Unattended operation** — a logon scheduled task runs a watcher that starts/stops the monitor together with TEMS.
- **Single-instance protection** — Windows named mutexes prevent duplicate watchers or monitors.

---

## How it works

```text
Windows logon
    │
    ▼
Task Scheduler: "TEMS Monitor Autostart"
    │  pythonw autostart.py
    ▼
autostart.py (watcher)
    │  waits for DiscoveryCore.exe
    ▼
monitor.py  ──────────────►  TEMS Discovery
    │  UI Automation / OCR        (Task Window)
    │
    ├── WhatsApp notifications (GREEN-API)
    │
    └── remote.py (polled every N seconds)
            │
            └── drives the real "Import Drive Test Data" dialog
```

1. `autostart.py` polls for `DiscoveryCore.exe`.
2. When TEMS is running, it launches `monitor.py` hidden.
3. `monitor.py` finds the TEMS window, reads the Task Window status line, and sends notifications when the state changes.
4. In parallel, `monitor.py` calls `remote.poll()`, which fetches WhatsApp notifications from GREEN-API and executes commands.
5. When TEMS closes, the watcher stops the monitor it started.

---

## Project structure

| File | Purpose |
|---|---|
| `monitor.py` | Main monitor: TEMS UI reading, OCR fallback, notifications, motivation messages, remote polling, single-instance guard. |
| `remote.py` | WhatsApp command handler, folder browsing, file selection, and UI automation of the TEMS import dialog. |
| `autostart.py` | Watcher that starts/stops `monitor.py` with TEMS. |
| `setup_autostart.ps1` | Registers the logon scheduled task for `autostart.py`. |
| `run_hidden.bat` | Convenience launcher: `pythonw monitor.py` (no console window). |
| `config.json` | Non-secret runtime configuration (safe to commit). |
| `config.example.json` | Sanitized configuration template for GitHub. |
| `.env` | Personal data and secrets — phone numbers, tokens, root path (**never commit**). |
| `.env.example` | Documented template for `.env` (safe to commit). |
| `requirements.txt` | Python dependencies. |
| `monitor.log` | Monitor runtime log (generated). |
| `autostart.log` | Watcher runtime log (generated). |
| `motivation_last.json` | Timestamp of the last motivation message (generated). |
| `imported.json` | File keys already imported by `/recent` (generated, ignored). |

---

## Requirements

- **Windows 10 or 11** (interactive desktop session; UI Automation does not work from a Windows service).
- **TEMS Discovery** installed and running with an **English UI** (the automation matches English window/button titles).
- **Python 3.10+** (64-bit recommended).
- A **WhatsApp number** linked to a GREEN-API instance (free Developer plan is enough for light use).
- Network access to `https://api.greenapi.com` / your instance host.

---

## Installation

```powershell
# 1. Clone / copy the project
cd "D:\Programming\Task - Project\files"

# 2. Create a virtual environment (recommended)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks script activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Create your configuration and your private environment file:

```powershell
Copy-Item config.example.json config.json
Copy-Item .env.example .env
notepad .env
```

All personal data goes into `.env`: your phone number, GREEN-API instance ID and
token, Telegram credentials, the local working root, and AI keys. `config.json`
holds only non-secret behaviour settings and can be committed safely.

Precedence (highest wins):

```text
real environment variables  >  .env  >  config.json
```

Three files are needed for a fresh clone: `config.json`, `.env`, and the Python
dependencies. If `config.json` is missing, the monitor falls back to
`config.example.json` and still reads everything from `.env`.

---

## Configuration

### Environment file (`.env`)

Copy `.env.example` to `.env` and fill in your values. Keys and mapping to the
runtime config:

| `.env` key | Overrides | Description |
|---|---|---|
| `DISPLAY_NAME` | `display_name` | Name used in messages. |
| `SENDER_TITLE` | `sender_title` | Sender title used in messages. |
| `CHANNEL` | `channel` | `telegram`, `whatsapp`, or `both`. |
| `WHATSAPP_PHONE` | `whatsapp.phone` | Your WhatsApp number, digits only. |
| `WHATSAPP_ID_INSTANCE` | `whatsapp.id_instance` | GREEN-API instance ID. |
| `WHATSAPP_API_TOKEN` | `whatsapp.api_token` | GREEN-API API token. |
| `WHATSAPP_API_URL` | `whatsapp.api_url` | Instance host. Blank = derived from the first 4 digits of the instance ID. |
| `WHATSAPP_FALLBACK_API_URL` | `whatsapp.fallback_api_url` | Fallback host, default `https://api.greenapi.com`. |
| `WHATSAPP_CHAT_ID` | `whatsapp.chat_id` | Optional explicit chat ID; blank = built from `WHATSAPP_PHONE`. |
| `WHATSAPP_PROVIDER` | `whatsapp.provider` | `greenapi`, `waha`, or `callmebot`. |
| `REMOTE_ROOT` | `remote.root` | Local root folder used to infer the project name, e.g. `D:\WORK`. |
| `REMOTE_ALLOWED_PHONES` | `remote.allowed_phones` | Comma-separated extra allowed numbers. |
| `REMOTE_ENABLED` | `remote.enabled` | `true`/`false`. |
| `TELEGRAM_TOKEN` | `telegram_token` | Telegram bot token. |
| `TELEGRAM_CHAT_ID` | `telegram_chat_id` | Telegram target chat. |
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | motivation API key | Optional AI motivation key. |
| `OPENAI_BASE_URL` | motivation base URL | API endpoint. |
| `CHAT_MODEL` | motivation model | Model name. |

`.env` accepts `export KEY=value`, `KEY="value"`, comments, and blank lines.

`config.json` reference (non-secret settings):

### General

| Key | Type | Description |
|---|---|---|
| `channel` | string | `telegram`, `whatsapp`, or `both`. |
| `window_title_regex` | string | Regex used to find the TEMS window. Default: `TEMS Discovery.*`. |
| `poll_seconds` | number | How often TEMS is checked (seconds). Default: `3`. |
| `window_missing_polls` | number | Consecutive missing-window polls before a warning/cleanup. Default: `5`. |
| `batch_settle_seconds` | number | Wait time to group files into one import batch. Default: `5`. |
| `display_name` | string | Name used in the notification template. |
| `sender_title` | string | Sender name used in the notification template. |
| `verify_ssl` | bool | TLS verification. Set `false` only on trusted networks with SSL inspection. |
| `ca_bundle` | string | Optional path to a custom CA bundle. |
| `use_ocr` | bool | Enable OCR fallback when UI Automation text is unavailable. |
| `msg_style` | string | `simple` (short text) or `letter` (formal Arabic letter template). |
| `msg_start` | string | Start phrase, e.g. `Tems Discovery start Loading Log Files`. |
| `msg_finish` | string | Finish phrase, e.g. `Finish. File Loading`. |

### Motivation

| Key | Type | Description |
|---|---|---|
| `motivation.enabled` | bool | Enable scheduled messages. |
| `motivation.every_hours` | number | Interval in hours. Default: `2`. |
| `motivation.provider` | string | `openrouter`, `openai`, or `local`. |
| `motivation.model` | string | Chat model, e.g. `openai/gpt-4o-mini`. |
| `motivation.env_file` | string | Path to a `.env` file (relative to the project). |
| `motivation.api_key` | string | Optional direct API key (prefer `.env`). |
| `motivation.base_url` | string | Optional custom API base URL. |

`.env` keys read by the motivation module:

```dotenv
OPENROUTER_API_KEY=sk-or-...
# or
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://openrouter.ai/api/v1
CHAT_MODEL=openai/gpt-4o-mini
```

If no key is configured, a built-in local message list is used.

### Autostart

| Key | Type | Description |
|---|---|---|
| `autostart.process_names` | array | Process names that trigger the monitor. Default: `["DiscoveryCore.exe"]`. |
| `autostart.poll_seconds` | number | Watcher polling interval. Default: `3`. |
| `autostart.stop_with_tems` | bool | Stop the monitor when TEMS closes. Default: `true`. |
| `autostart.python` | string | Optional explicit `python.exe` path. Empty = auto-detect. |

### Remote control

| Key | Type | Description |
|---|---|---|
| `remote.enabled` | bool | Enable WhatsApp remote commands. |
| `remote.poll_seconds` | number | Notification polling interval. Default: `20`. Keep it high on the free plan. |
| `remote.root` | string | Root folder used to infer the project name, e.g. `D:\WORK`. |
| `remote.page_size` | number | Items per listing page. Default: `18`. |
| `remote.max_files` | number | Maximum files imported by `/all`. Default: `100`. |
| `remote.dataset` | string | Default target dataset. Empty = leave TEMS value unchanged. |
| `remote.allowed_phones` | array | Extra allowed sender numbers (digits only). `whatsapp.phone` is always allowed. |
| `remote.search_roots` | array | Extra roots for the automatic recent-log scan. Empty = `remote.root`, Desktop, Downloads, Documents. |
| `remote.recent_days` | number | Only `.trp` files newer than this many days are "recent". Default: `7`. |
| `remote.scan_depth` | number | Maximum folder depth for the scan. Default: `4`. |
| `remote.recent_count` | number | How many recent folders `/start` / `/recent` show. Default: `3`. |
| `remote.scan_seconds` | number | Time budget for the scan. Default: `8`. |
| `remote.stale_seconds` | number | Ignore notifications older than this. Default: `180`. |

### WhatsApp

| Key | Type | Description |
|---|---|---|
| `whatsapp.provider` | string | `greenapi`, `waha`, or `callmebot`. |
| `whatsapp.phone` | string | Destination number in international format, digits only (e.g. `201XXXXXXXXX`). |
| `whatsapp.id_instance` | string | GREEN-API instance ID. |
| `whatsapp.api_token` | string | GREEN-API API token. |
| `whatsapp.api_url` | string | Instance API host, e.g. `https://7107.api.greenapi.com`. |
| `whatsapp.fallback_api_url` | string | Fallback host, e.g. `https://api.greenapi.com`. |
| `whatsapp.chat_id` | string | Optional explicit chat ID (otherwise derived from `phone`). |
| `whatsapp.waha_session` | string | WAHA session name (WAHA only). |

### Telegram (optional)

| Key | Type | Description |
|---|---|---|
| `telegram_token` | string | Bot token from BotFather. |
| `telegram_chat_id` | string | Target chat ID. |

---

## WhatsApp / GREEN-API setup

1. Create an instance at [green-api.com](https://green-api.com/) and link your WhatsApp number by scanning the QR code.
2. Copy the **instance ID** and **API token** into `.env` (`WHATSAPP_ID_INSTANCE` and `WHATSAPP_API_TOKEN`).
3. Enable the notification types used by this project:

```powershell
# Run once; replace the host, instance ID and token
$host = "https://api.greenapi.com"
$id   = "YOUR_INSTANCE_ID"
$tok  = "YOUR_API_TOKEN"

Invoke-RestMethod -Method Post `
  -Uri "$host/waInstance$id/setSettings/$tok" `
  -ContentType "application/json" `
  -Body '{"webhookUrl":"","incomingWebhook":"yes","outgoingMessageWebhook":"yes","outgoingAPIMessageWebhook":"no"}'
```

| Setting | Value | Why |
|---|---|---|
| `webhookUrl` | `""` | Use HTTP polling (`receiveNotification`), not a webhook server. |
| `incomingWebhook` | `"yes"` | Receive messages sent **to** the bot number. |
| `outgoingMessageWebhook` | `"yes"` | Receive commands typed on the **linked phone** (self-chat). |
| `outgoingAPIMessageWebhook` | `"no"` | Prevent the bot's own API replies from being treated as commands. |

> GREEN-API applies `setSettings` within about 5 minutes and reboots the instance.

Verify:

```powershell
Invoke-RestMethod "$host/waInstance$id/getSettings/$tok" | ConvertTo-Json
```

---

## Running the monitor

### Foreground (for testing)

```powershell
python monitor.py
```

### Hidden (no console window)

```powershell
.\run_hidden.bat
```

or:

```powershell
pythonw monitor.py
```

### Utility commands

| Command | Purpose |
|---|---|
| `python monitor.py --test` | Send a test notification and exit. |
| `python monitor.py --dump` | Print the text elements the monitor can read from TEMS. |
| `python monitor.py --motivate` | Generate and send one motivation message. |
| `python monitor.py --chatid` | Print chat IDs that messaged your Telegram bot. |
| `python autostart.py --status` | Show TEMS PIDs, monitor state, and watcher state. |
| `python remote.py --green-check` | Inspect the GREEN-API instance state/settings and linked WhatsApp mode. Add `--fix` to apply the recommended settings. |

The monitor refuses to start twice; only one instance per Windows session is allowed.

---

## Remote control from WhatsApp

Send these messages in the WhatsApp chat linked to the bot:

| Command | Action |
|---|---|
| `/start` | Begin browsing from **This PC**; also lists newly found `.trp` folders as ⭐. |
| `/recent` | Show only folders with new, not-yet-imported `.trp` files. |
| `1`, `2`, … | Open the numbered folder or select the numbered file. |
| A folder name (e.g. `WORK`) | Open that folder by name. |
| `..` or `/up` | Go up one level. |
| `/next` / `/prev` | Next / previous page. |
| `/all` | Select all `.trp` files in the current folder (capped by `max_files`). |
| `/all 20` | Select the newest 20 `.trp` files. |
| `/yes` | Confirm and start the import. |
| `/project <name>` | Override the inferred TEMS project. |
| `/dataset <name>` | Override the target dataset. |
| `/status` | Show the current TEMS import status. |
| `/cancel` | Cancel the current remote session. |
| `/help` | Show the command list. |

### Example session

```text
You:  /start
Bot:  📂 💻 This PC
      1) 📁 C:\
      2) 📁 D:\

You:  2
Bot:  📂 D:\
      1) 📁 AlphaCode
      ...
      20) 📁 WORK

You:  WORK
Bot:  📂 D:\WORK
      1) 📁 Dayrout Subzone
      ...

You:  1
Bot:  📂 D:\WORK\Dayrout Subzone
      ...

You:  2
Bot:  📦 Ready to import:
      📄 20 .trp files
      (out of 51 in this folder)
      📂 D:\WORK\Dayrout Subzone\...
      🎯 Project: Dayrout Subzone
      🎯 DataSet: (unchanged)
      Send /yes to confirm or /cancel

You:  /yes
Bot:  🚀 Import started: 20 files.
```

**File-count rule:** only `.trp` files directly inside the selected folder are counted, sorted by modification time (newest first). The number shown in the “Ready to import” message is exactly the number sent to TEMS.

**Import automation:** the tool opens TEMS' **Import Drive Test Data** dialog, adds the selected files through the file picker, selects the project/dataset combo boxes by name, and clicks **Import**. TEMS must be running and idle (no other import dialog open).

---

## Automatic start with TEMS

The project can run fully unattended: at Windows logon, a scheduled task starts `autostart.py`, which waits for TEMS and launches the monitor.

### Register the scheduled task

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_autostart.ps1
```

This creates a task named **TEMS Monitor Autostart** with an *At logon* trigger for the current user. It runs:

```text
pythonw.exe "D:\...\files\autostart.py"
```

### Check status

```powershell
python autostart.py --status
```

Example output:

```text
TEMS pids: [11384, 12960, 17568, 19032]
monitor running: True
watcher running: True
```

### Behaviour

- TEMS opens → the watcher starts `monitor.py` hidden within a few seconds.
- TEMS closes → the watcher stops the monitor it started (`stop_with_tems: true`).
- The monitor and watcher each use a Windows named mutex, so duplicate copies exit immediately.
- If TEMS is already running when the watcher starts, it starts the monitor immediately.

### Uninstall autostart

```powershell
Stop-Process -Name pythonw -Force
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'monitor\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Unregister-ScheduledTask -TaskName "TEMS Monitor Autostart" -Confirm:$false
```

---

## Logs

| File | Contents |
|---|---|
| `monitor.log` | TEMS status changes, notifications, remote commands, API errors. |
| `autostart.log` | Watcher start/stop events and monitor launches. |

Useful log lines:

| Line | Meaning |
|---|---|
| `Monitoring started on <host>` | Monitor is running. |
| `Notification received: incomingMessageReceived` | A WhatsApp message reached the queue. |
| `Incoming text [...] from ...: /start` | A command was parsed. |
| `Remote command [...]` | A command was executed. |
| `Remote poll error (N in a row): ...` | GREEN-API polling failed (usually transient). |
| `Could not deliver WhatsApp message` | Sending failed after retries. |
| `Task Window summary line not found` | OCR/UIA could not read the Task Window; make it visible. |

---

## Troubleshooting

### `/start` gets no reply

1. Confirm the instance settings:

   ```powershell
   Invoke-RestMethod "$host/waInstance$id/getSettings/$tok" | ConvertTo-Json
   ```

   `incomingWebhook` must be `yes`. If you type commands on the **linked phone** (self-chat), `outgoingMessageWebhook` must also be `yes`.

2. Check `monitor.log` for `Notification received` / `Incoming text`. If no notification ever arrives, GREEN-API is not queueing the message.
3. Make sure the sender is allowed:
   - `WHATSAPP_PHONE` (digits only), or
   - one of `REMOTE_ALLOWED_PHONES` in `.env`.
4. Look for `Ignored command from ...` in the log; add that number to `REMOTE_ALLOWED_PHONES` in `.env`.
5. Command-like text is only processed up to 160 characters.

### `quotaExceeded` notifications

The GREEN-API **Developer** plan has request/message limits. Constant polling consumes them quickly.

- Keep `remote.poll_seconds` at `20` or higher.
- Do not run multiple monitor instances.
- The free plan allows only **3 chats per month**; commands from other chats stop arriving when the limit is hit. `python remote.py --green-check` shows which chats are currently allowed.
- Upgrade the GREEN-API plan or reduce notification volume.

### `Remote poll error ... ConnectTimeout` / `ReadTimeout`

Transient network issues. The client automatically retries the configured `fallback_api_url`. If it persists:

- Check DNS/HTTPS access to the GREEN-API hosts.
- Increase `remote.poll_seconds`.
- Check proxy/antivirus TLS inspection; optionally set `verify_ssl: false` only on trusted networks.

### `Task Window summary line not found`

- Open the **Task Window** tab in TEMS and keep it visible.
- Run `python monitor.py --dump` to see what the monitor can read.
- Ensure `use_ocr` is `true` if the status line is custom-drawn.

### Import does not start

- TEMS must be open and no other **Import Drive Test Data** dialog may be open.
- Project/dataset names must match the TEMS combo-box items **exactly** (case-insensitive).
- The TEMS UI must be in English (the automation uses English titles/auto IDs).
- The monitor must run in the same interactive desktop session as TEMS.

### Duplicate instances

The named mutexes prevent duplicates. Check with:

```powershell
python autostart.py --status
```

If a stale process remains, stop all `pythonw`/`monitor.py` processes and restart the watcher.

---

## Security and privacy

- **`.env` contains all personal data and secrets** (phone numbers, GREEN-API ID/token, Telegram token, local paths, AI keys). Never commit or share it. It is listed in `.gitignore`.
- `config.json` is sanitized (no tokens or phone numbers) and holds only behaviour settings; it is still ignored by default as a safety measure. If you choose to commit it, check it first.
- Rotate the GREEN-API token immediately if it is exposed.
- Keep `REMOTE_ALLOWED_PHONES` restricted to your own numbers.
- Remote control can browse local drives and start imports. Run this tool only on a trusted, personal machine.
- The tool reads TEMS UI text only; it does not modify `.trp` files directly. Imports are performed by TEMS itself.

---

## Publishing to GitHub

The repository is ready to publish: secrets live in `.env`, which is ignored.
Before pushing:

```powershell
# 1. Check that .env is ignored (it must never be staged)
git check-ignore -v .env

# 2. On a fresh machine, create your private files from the templates
Copy-Item config.example.json config.json
Copy-Item .env.example .env
# edit .env with your own values

# 3. Initialise the repository
git init
git add .
git commit -m "TEMS Discovery import monitor with WhatsApp remote control"

# 4. Create an empty repository on GitHub, then:
git remote add origin https://github.com/<your-user>/<your-repo>.git
git branch -M main
git push -u origin main
```

Recommended `.gitignore` entries (already included):

```gitignore
config.json
.env
*.log
__pycache__/
*.pyc
motivation_last.json
imported.json
.venv/
```

> If you ever committed a real token (in `.env` or `config.json`), remove it from history and rotate the token. A `.gitignore` alone does not erase past commits.

Anyone cloning only needs to create `.env` from `.env.example`; they do **not**
get your phone number or credentials.

---

## Disclaimer

This project is an independent automation utility and is **not affiliated with or endorsed by Infovista / TEMS**. TEMS Discovery is a commercial product; use this tool in accordance with your license and your organization's policies.

The software is provided “as is”, without warranty of any kind. You are responsible for the imports it triggers and for any data it processes.
