# wechat-assistant

> description: "WX Echo — 微信 AI 个人助手：自动从微信聊天中提取待办、日程、干货，推送到 Discord Forum，闭环追踪任务状态"

## Triggers

- wx echo
- wechat assistant
- 微信助手
- 设置微信助手
- 微信待办
- 微信日程
- 微信干货
- wechat todo
- wechat calendar
- wechat digest

## Prerequisites

- **macOS** 或 **Windows** + 微信桌面版 4.0+
- **Python 3.8+** + `pip3 install pycryptodome zstandard pyyaml`
- **OpenClaw** 已配置 Discord（需开启社区功能以支持 Forum 频道）
- Python 依赖：`pycryptodome`, `zstandard`, `pyyaml`

## Architecture

两层设计：

### Layer 1: 独立 CLI 工具（scripts/ 目录）
纯 Python，不依赖 OpenClaw，任何人都能用。只做数据提取输出 JSON，**不调 AI API**。

### Layer 2: OpenClaw Skill（本文件 + prompts/）
Agent 读 prompt 模板，调 CLI 拿 JSON，分析后推送到 Discord。

## File Structure

```
scripts/
  decrypt/
    find_all_keys_macos.c   — C 密钥提取源码（macOS）
    decrypt_db.py            — 数据库全量解密（--config）
    config.py                — 配置加载器
  refresh_decrypt.py         — 增量解密（WAL patch，cron 用这个）
  collector.py               — 一次性增量同步命令
  extract_todos.py           — 提取私聊对话 → JSON
  extract_calendar.py        — 提取日程相关对话 → JSON
  extract_digest.py          — 提取群聊消息 → JSON
  requirements.txt           — Python 依赖
prompts/
  todo-scan.md               — 待办扫描 cron prompt 模板
  calendar-scan.md           — 日程扫描 cron prompt 模板
  digest.md                  — 干货收集 cron prompt 模板
config.example.yaml          — 配置模板
```

### 数据流

```
微信进程 → 加密DB + WAL（持续更新）
     ↓
refresh_decrypt.py（WAL patch，~70ms/DB）
     ↓
解密后 DB（持续更新）
     ↓
collector.py --sync（增量同步到 collector.db）
     ↓
extract_*.py（提取 JSON）→ Agent 分析 → Discord 推送
```

> **关键**：`refresh_decrypt.py` 是保持数据新鲜的核心。每次 cron 都先运行它，
> 它会检测 WAL 文件 mtime 变化，只解密新增的 WAL frame（通常 <1 秒），
> 而不是每次全量解密 19GB 数据。
> 参考实现：[bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt/tree/feat/macos-support)

## Setup（首次设置流程）

用户说"帮我设置微信助手"时，Agent 按以下步骤引导：

> **工作目录（`<work_dir>`）**：用户存放 config.yaml、all_keys.json、collector.db 等运行时文件的目录。
> 建议创建 `~/wechat-assistant`。Agent 引导时先询问用户想放哪里，默认 `~/wechat-assistant`。
> `<skill_dir>` 是 skill 安装目录（只读代码），`<work_dir>` 是运行时数据目录（可写）。

### Step 1: 创建工作目录 + 安装依赖

```bash
mkdir -p ~/wechat-assistant && cd ~/wechat-assistant
pip3 install pycryptodome zstandard pyyaml
```

### Step 2: 编译密钥提取工具（macOS）

```bash
cd <skill_dir>/scripts/decrypt
cc -O2 -o find_all_keys_macos find_all_keys_macos.c
```

> 编译不需要 sudo。运行密钥提取时才需要 sudo。微信桌面版必须正在运行。

### Step 3: 提取密钥

```bash
cd <work_dir>
sudo <skill_dir>/scripts/decrypt/find_all_keys_macos
# 输出 all_keys.json 到当前目录
```

> 必须 `cd` 到 config.yaml 所在的目录再运行，因为 `all_keys.json` 会输出到当前工作目录，
> 而 config.yaml 默认配置 `keys_file: "./all_keys.json"` 是相对于 config.yaml 解析的。

### Step 4: 创建配置文件

```bash
cp <skill_dir>/config.example.yaml <work_dir>/config.yaml
```

引导用户填写：
- `wechat.db_dir` — 微信数据库目录（macOS 自动检测路径：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage`，取 `*` 部分中含 wxid 的那个）
- `wechat.self_wxid` — 用户的微信 wxid（从 `db_dir` 路径中提取，格式如 `wxid_xxxxxxxxxxxx`；或在解密后查 `contact.db` 中 `type=0` 的记录）
- `monitor.groups` — 想监控的群 chatroom_id（格式如 `12345678@chatroom`；可在首次同步后用 `sqlite3 collector.db "SELECT chatroom_id, chatroom_name FROM watched_chats WHERE chatroom_id LIKE '%@chatroom' LIMIT 20"` 查看）
- `monitor.work_groups` — 工作群映射（日程扫描用，非 @chatroom 格式的工作群也会保留）

### Step 5: 解密数据库

```bash
cd <skill_dir>/scripts
python3 decrypt/decrypt_db.py --config <config_path>
```

### Step 6: 首次采集

```bash
# 首次同步（自动发现所有群和私聊）
python3 <skill_dir>/scripts/collector.py --config <config_path> --sync
```

> `--sync` 首次运行时如果 watched_chats 为空，会自动触发 discover（扫描 session.db 和 contact.db）。
> ⚠️ 首次同步会注册所有历史会话，可能较慢。同步完成后可用：
> `sqlite3 <work_dir>/collector.db "SELECT chatroom_id, chatroom_name FROM watched_chats LIMIT 20"`
> 查看已注册的会话，手动删除不需要的。

### Step 7: 创建 Discord 频道

> ⚠️ Forum 频道需要服务器已开启"社区"功能（服务器设置 → 启用社区 → 开启论坛频道）。

在用户指定的 Discord 类目下创建 **1 个 Forum 频道**：

**echo-微信助手**
- 使用 `message(action="channel-create", name="echo-微信助手", type=15)` 创建（type 15 = Forum）
- 创建后添加 tags：`📋待办`、`📅日程`、`📰干货`

> **闭环机制**：
> - **待办**：每次 cron 扫描微信对话，自动追踪 open 待办的完成信号（对方说"搞定了"等）→ 回复原帖 + 关帖 + 更新 todos.json。对方催促时自动追加提醒，超时 7 天自动提醒未跟进。**用户也可以手动关闭帖子表示已完成**，下次 cron 会自动检测并更新状态。
> - **日程**：事件时间过期后自动回复"日程已过"并关帖。
> - **干货**：每日一帖，Forum 本身即归档，无需额外同步。

### Step 8: 注册 Cron 任务

注册 3 个 cron 任务（使用 `cron action=add`）。每个 cron 的 `agentTurn` 内容来自 `prompts/` 目录下的模板，注册时替换以下占位符：

| 占位符 | 含义 |
|--------|------|
| `{{config_path}}` | config.yaml 的绝对路径 |
| `{{skill_dir}}` | skill 根目录绝对路径 |
| `{{forum_id}}` | echo-微信助手 Forum 频道 ID |
| `{{groups}}` | 监控群 ID 列表（逗号分隔） |
| `{{ssh_host}}` | 微信所在机器的 SSH 地址（如果微信不在本机，留空表示本机直接执行） |
| `{{ssh_password}}` | SSH 密码（配合 sshpass 使用） |

> **本机 vs 远程**：如果 OpenClaw 和微信在同一台机器上（常见情况），不需要 SSH，prompt 中的命令直接本地执行。只有 OpenClaw 在另一台机器时才需要 SSH。Agent 注册 cron 时根据情况决定是否包含 SSH 前缀。

#### Cron 1: 待办扫描（每 30 分钟）
- 模板：`prompts/todo-scan.md`
- Schedule: `*/30 * * * *`

#### Cron 2: 日程扫描（每 30 分钟，8-23 点）
- 模板：`prompts/calendar-scan.md`
- Schedule: `*/30 8-23 * * *`

#### Cron 3: 干货收集（每天 9:00）
- 模板：`prompts/digest.md`
- Schedule: `0 9 * * *`

## CLI Usage

所有 CLI 工具都是一次性命令，执行完退出。

### 增量解密（每次 cron 必须先跑）

```bash
# 正常模式：检测 WAL 变化，只 patch 新页面（<1 秒）
python3 refresh_decrypt.py --config config.yaml

# 强制全量解密
python3 refresh_decrypt.py --config config.yaml --full
```

### 同步消息

```bash
# 同步所有 watched_chats
python3 collector.py --config config.yaml --sync

# 只同步单个群
python3 collector.py --config config.yaml --sync --chatroom 12345@chatroom
```

### 提取私聊待办数据

```bash
# 增量（最近 35 分钟）
python3 extract_todos.py --config config.yaml

# 全量（昨天整天）
python3 extract_todos.py --config config.yaml --full
```

### 提取日程数据

```bash
# 增量
python3 extract_calendar.py --config config.yaml

# 全量
python3 extract_calendar.py --config config.yaml --full
```

### 提取群聊干货

```bash
# 默认：config 中所有 monitor.groups，昨天
python3 extract_digest.py --config config.yaml

# 指定群和日期
python3 extract_digest.py --config config.yaml --groups "123@chatroom,456@chatroom" --date 2026-03-12
```

## Maintenance

### 微信重启后

密钥会变，需要重新执行 Step 3（提取密钥）。
然后运行 `refresh_decrypt.py --full` 强制全量解密。
Agent 如果发现 `refresh_decrypt.py` 报错（HMAC 验证失败），应主动提醒用户重新提取密钥。

### WAL checkpoint 后

微信会定期将 WAL 合并到主 DB（checkpoint）。此时 `refresh_decrypt.py` 会自动检测到主 DB 的 mtime 变化，触发全量解密。无需手动操作。

### 添加/移除监控群

编辑 `config.yaml` 的 `monitor.groups`，下次 cron 自动生效。

## Security Notes

- `all_keys.json` 包含数据库加密密钥，**不要泄露或提交到 Git**
- `config.yaml` 包含路径配置，同样需要保密
- 密钥提取需要 **sudo** 权限
