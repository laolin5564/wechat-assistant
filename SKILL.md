---
name: "wx-echo"
description: "Extract todos, calendar events, and curated content from WeChat desktop chats via local SQLCipher 4 decryption, then push structured updates to a Discord Forum channel with closed-loop status tracking. Use when the user asks to set up a WeChat assistant, scan WeChat for todos or calendar items, collect group chat highlights, or automate WeChat-to-Discord message forwarding."
user-invocable: true
triggers:
  - "wx echo"
  - "wechat assistant"
  - "微信助手"
  - "设置微信助手"
  - "微信待办"
  - "微信日程"
  - "微信干货"
  - "wechat todo"
  - "wechat calendar"
  - "wechat digest"
---

## Prerequisites

- **macOS** or **Windows** + WeChat Desktop 4.0+
- **Python 3.8+** with `pip3 install pycryptodome zstandard pyyaml`
- **OpenClaw** configured with Discord (community features enabled for Forum channels)

## Architecture

Two-layer design separating data extraction from AI analysis:

### Layer 1: Standalone CLI Tools (`scripts/`)

Pure Python, no OpenClaw dependency. Extracts data and outputs JSON — never calls AI APIs.

### Layer 2: OpenClaw Skill (this file + `prompts/`)

The agent reads prompt templates, invokes CLI tools to get JSON, analyzes results, and pushes to Discord.

To modify analysis logic, edit `prompts/*.md` without touching code.

## File Structure

```
scripts/
  decrypt/
    find_all_keys_macos.c   — C key extraction from WeChat process memory (macOS, requires sudo)
    decrypt_db.py            — Full database decryption (first-time use, --config)
    config.py                — YAML config loader
  refresh_decrypt.py         — Incremental decryption (WAL patch, used by cron)
  collector.py               — One-shot incremental message sync
  extract_todos.py           — Extract private chat conversations → JSON
  extract_calendar.py        — Extract calendar-related conversations → JSON
  extract_digest.py          — Extract group chat messages → JSON
  requirements.txt           — Python dependencies
prompts/
  todo-scan.md               — Todo scanning cron prompt template
  calendar-scan.md           — Calendar scanning cron prompt template
  digest.md                  — Content curation cron prompt template
config.example.yaml          — Configuration template
```

### Data Flow

```
WeChat process → Encrypted DB + WAL (continuously updated)
     ↓
refresh_decrypt.py (WAL patch, ~70ms per DB)
     ↓
Decrypted DB (continuously updated)
     ↓
collector.py --sync (incremental sync to collector.db)
     ↓
extract_*.py (output JSON) → Agent analysis → Discord push
```

> **Key**: `refresh_decrypt.py` keeps data fresh. Each cron run executes it first — it detects WAL file mtime changes and decrypts only new WAL frames (typically <1 second), avoiding full 19GB decryption each time.
> Reference implementation: [bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt/tree/feat/macos-support)

## Setup (First-Time Flow)

When the user asks to set up a WeChat assistant, the agent guides through these steps:

> **Working directory (`<work_dir>`)**: Where config.yaml, all_keys.json, collector.db and other runtime files live. Suggest `~/wechat-assistant`. Ask the user where they prefer; default to `~/wechat-assistant`.
> **`<skill_dir>`** is the skill install directory (read-only code). **`<work_dir>`** is the runtime data directory (writable).

### Step 1: Create Working Directory + Install Dependencies

```bash
mkdir -p ~/wechat-assistant && cd ~/wechat-assistant
pip3 install pycryptodome zstandard pyyaml
```

### Step 2: Compile Key Extraction Tool (macOS)

```bash
cd <skill_dir>/scripts/decrypt
cc -O2 -o find_all_keys_macos find_all_keys_macos.c
```

> Compilation does not require sudo. Running key extraction does. WeChat Desktop must be running.

### Step 3: Extract Keys

```bash
cd <work_dir>
sudo <skill_dir>/scripts/decrypt/find_all_keys_macos
# Outputs all_keys.json to current directory
```

> Run from the config.yaml directory because `all_keys.json` outputs to CWD, and config.yaml references `keys_file: "./all_keys.json"` relative to itself.

### Step 4: Create Configuration File

```bash
cp <skill_dir>/config.example.yaml <work_dir>/config.yaml
```

Guide the user to fill in:
- `wechat.db_dir` — WeChat database directory (macOS auto-detect path: `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage`, use the `*` segment containing wxid)
- `wechat.self_wxid` — User's WeChat wxid (extract from `db_dir` path, format: `wxid_xxxxxxxxxxxx`; or query `contact.db` for `type=0` record after decryption)
- `monitor.groups` — Group chatroom IDs to monitor (format: `12345678@chatroom`; discoverable after first sync via `sqlite3 collector.db "SELECT chatroom_id, chatroom_name FROM watched_chats WHERE chatroom_id LIKE '%@chatroom' LIMIT 20"`)
- `monitor.work_groups` — Work group mapping (for calendar scanning; non-@chatroom format work groups are also preserved)

### Step 5: Decrypt Databases

```bash
cd <skill_dir>/scripts
python3 decrypt/decrypt_db.py --config <config_path>
```

### Step 6: First Collection

```bash
# First sync (auto-discovers all groups and private chats)
python3 <skill_dir>/scripts/collector.py --config <config_path> --sync
```

> `--sync` automatically triggers discovery when `watched_chats` is empty on first run (scans session.db and contact.db).
> First sync registers all historical conversations and may be slow. After completion, review with:
> `sqlite3 <work_dir>/collector.db "SELECT chatroom_id, chatroom_name FROM watched_chats LIMIT 20"`
> and manually remove unwanted entries.

### Step 7: Create Discord Channel

> Forum channels require the server to have "Community" enabled (Server Settings → Enable Community → enable Forum channels).

Create **1 Forum channel** under the user's specified Discord category:

**echo-微信助手**
- Use `message(action="channel-create", name="echo-微信助手", type=15)` to create (type 15 = Forum)
- Add tags after creation: `📋待办`, `📅日程`, `📰干货`

> **Closed-loop mechanisms:**
> - **Todos**: Each cron scan checks WeChat conversations for completion signals on open todos (e.g., "搞定了") → replies to original thread + closes thread + updates todos.json. Overdue items (7+ days) trigger automatic reminders. Users can also manually close threads to mark completion — the next cron detects this and updates state.
> - **Calendar**: Auto-replies "日程已过" and closes thread when event time passes.
> - **Curated content**: One thread per day; the Forum itself serves as archive.

### Step 8: Register Cron Tasks

Register 3 cron tasks using `cron action=add`. Each cron's `agentTurn` content comes from templates in `prompts/`, with these placeholders replaced at registration:

| Placeholder | Meaning |
|-------------|---------|
| `{{config_path}}` | Absolute path to config.yaml |
| `{{skill_dir}}` | Skill root directory absolute path |
| `{{forum_id}}` | echo-微信助手 Forum channel ID |
| `{{groups}}` | Monitored group IDs (comma-separated) |
| `{{ssh_host}}` | SSH address for WeChat machine (empty = local execution) |
| `{{ssh_password}}` | SSH password (used with sshpass) |

> **Local vs remote**: When OpenClaw and WeChat run on the same machine (common case), SSH is unnecessary — commands execute locally. SSH is only needed when OpenClaw runs on a different machine. The agent decides whether to include SSH prefixes when registering cron tasks.

#### Cron 1: Todo Scan (every 30 minutes)
- Template: `prompts/todo-scan.md`
- Schedule: `*/30 * * * *`

#### Cron 2: Calendar Scan (every 30 minutes, 8AM–11PM)
- Template: `prompts/calendar-scan.md`
- Schedule: `*/30 8-23 * * *`

#### Cron 3: Content Curation (daily at 9:00)
- Template: `prompts/digest.md`
- Schedule: `0 9 * * *`

## CLI Usage

All CLI tools are one-shot commands that exit after execution.

### Incremental Decryption (run before every cron)

```bash
# Normal mode: detect WAL changes, patch only new pages (<1 second)
python3 refresh_decrypt.py --config config.yaml

# Force full decryption
python3 refresh_decrypt.py --config config.yaml --full
```

### Sync Messages

```bash
# Sync all watched_chats
python3 collector.py --config config.yaml --sync

# Sync a single group only
python3 collector.py --config config.yaml --sync --chatroom 12345@chatroom
```

### Extract Private Chat Todos

```bash
# Incremental (last 35 minutes)
python3 extract_todos.py --config config.yaml

# Full (entire previous day)
python3 extract_todos.py --config config.yaml --full
```

### Extract Calendar Events

```bash
# Incremental
python3 extract_calendar.py --config config.yaml

# Full
python3 extract_calendar.py --config config.yaml --full
```

### Extract Group Chat Highlights

```bash
# Default: all monitor.groups from config, previous day
python3 extract_digest.py --config config.yaml

# Specify groups and date
python3 extract_digest.py --config config.yaml --groups "123@chatroom,456@chatroom" --date 2026-03-12
```

## Maintenance

### After WeChat Restart

Keys change on restart — re-run Step 3 (key extraction). Then run `refresh_decrypt.py --full` to force full decryption. If `refresh_decrypt.py` reports HMAC verification failure, proactively prompt the user to re-extract keys.

### After WAL Checkpoint

WeChat periodically merges WAL into the main DB (checkpoint). `refresh_decrypt.py` automatically detects main DB mtime changes and triggers full decryption. No manual action needed.

### Add/Remove Monitored Groups

Edit `monitor.groups` in `config.yaml`. Changes take effect on the next cron run.

## Security Notes

- `all_keys.json` contains database encryption keys — **never leak or commit to Git**
- `config.yaml` contains path configuration — also keep private
- Key extraction requires **sudo** privileges
