# 🦞 wechat-assistant

**微信 AI 个人助手** — 自动从微信聊天中提取待办、日程、干货，推送到你的工作流。

> ⚠️ **Alpha 半成品** — 核心链路已跑通，但未经大规模验证。
> 欢迎龙虾们在使用过程中提 Issue、提 PR，一起把这个东西做完善。

---

## 它能干什么

| 功能 | 数据源 | 输出 |
|------|--------|------|
| 📋 **待办提取** | 私聊对话 | "张三让你下午3点送文件" → Discord / 任何 webhook |
| 📅 **日程扫描** | 私聊 + 工作群 | "周五开产品评审" → Apple Calendar / Google Calendar |
| 📰 **干货收集** | 指定群聊 | AI 群里的工具推荐、技术分享 → 日报归档 |

**不依赖任何云服务**，数据全程本地处理。AI 分析部分可选（你可以只用 CLI 工具拿 JSON，自己处理）。

---

## 设计思路

### 为什么要做这个

微信是中国人的工作通讯主力，但它**没有 API**。大量待办、约会、有价值的信息散落在聊天记录里，靠人脑记忆容易漏。

微信 4.0 在本地用 SQLCipher 4 加密存储所有聊天记录。只要能解密，就能用程序读取。

### 架构：两层分离

```
┌─────────────────────────────────────────┐
│  Layer 2: AI Agent（可选）               │
│  读 prompt 模板 → 调 CLI → 分析 JSON    │
│  → 推送 Discord / Calendar / Webhook    │
│  适配: OpenClaw / Claude Code / 任何 Agent │
└─────────────────┬───────────────────────┘
                  │ 调用 CLI，读 JSON stdout
┌─────────────────▼───────────────────────┐
│  Layer 1: 独立 CLI 工具                  │
│  纯 Python，不依赖任何 AI 框架           │
│  解密 → 同步 → 提取 → 输出 JSON         │
└─────────────────────────────────────────┘
```

**Layer 1** 是独立的命令行工具，任何人都能用，不需要 AI。
**Layer 2** 是 AI Agent 集成层，目前适配了 [OpenClaw](https://github.com/openclaw/openclaw)，但设计上可以接任何 Agent。

### 数据流

```
微信进程
    │
    ├── 加密 DB (SQLCipher 4, AES-256-CBC, ~19GB)
    │       │
    │       ▼
    │   find_all_keys_macos ──── sudo 扫进程内存 ──→ all_keys.json
    │       │
    │       ▼
    │   decrypt_db.py ──── 首次全量解密 ──→ decrypted/*.db
    │
    └── WAL 文件 (预分配 4MB，30ms 级更新)
            │
            ▼
        refresh_decrypt.py ──── mtime 检测 + WAL patch ──→ 更新 decrypted/*.db
            │                   （增量，<1秒，cron 每次先跑）
            ▼
        collector.py --sync ──── 增量同步 ──→ collector.db (SQLite)
            │
            ├── extract_todos.py    → JSON (私聊待办)
            ├── extract_calendar.py → JSON (日程事件)
            └── extract_digest.py   → JSON (群聊干货)
                    │
                    ▼
                AI Agent 分析 JSON → 推送到你的工作流
```

### 关键设计决策

1. **解密后存标准 SQLite**，不依赖 SQLCipher 库（安装难、跨平台差）
2. **WAL 增量解密**（来自 [bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt)）避免每次全量解密 19GB
3. **CLI 输出 JSON 到 stdout**，不调 AI API — Agent 侧的事交给 Agent
4. **HMAC 验证**在全量解密前校验密钥，微信重启后密钥失效能立即发现（exit code 2）

---

## 快速开始

### 环境要求

- **macOS 13+**（ARM64 或 Intel）
- **微信桌面版 4.0+**（正在运行）
- **Python 3.10+**
- **sudo 权限**（仅密钥提取需要）

### 1. 安装依赖

```bash
pip3 install pycryptodome zstandard pyyaml
```

### 2. 编译密钥提取工具

```bash
cd scripts/decrypt
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
```

### 3. 创建工作目录 + 配置

```bash
mkdir ~/wechat-assistant && cd ~/wechat-assistant
cp /path/to/this/repo/config.example.yaml config.yaml
```

编辑 `config.yaml`：

```yaml
wechat:
  # 微信数据库目录（在微信设置 → 文件管理中找路径）
  db_dir: "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/你的WXID/db_storage"

  # 你的微信 wxid
  self_wxid: "wxid_xxxxxxxxxxxx"
```

### 4. 提取密钥（需要 sudo + 微信正在运行）

```bash
cd ~/wechat-assistant
sudo /path/to/scripts/decrypt/find_all_keys_macos
# 输出 all_keys.json 到当前目录
```

### 5. 首次全量解密

```bash
python3 /path/to/scripts/decrypt/decrypt_db.py --config config.yaml
```

> ⏱️ 首次解密取决于数据库大小，19GB 大约需要 2-5 分钟。

### 6. 首次采集

```bash
# 自动发现所有群聊和私聊
python3 /path/to/scripts/collector.py --config config.yaml --sync
```

### 7. 试一下提取

```bash
# 提取昨天的私聊待办数据
python3 /path/to/scripts/extract_todos.py --config config.yaml --full

# 提取昨天的群聊干货
python3 /path/to/scripts/extract_digest.py --config config.yaml --groups "你的群ID@chatroom"

# 提取日程相关对话
python3 /path/to/scripts/extract_calendar.py --config config.yaml --full
```

输出是 JSON，你可以 `| python3 -m json.tool` 格式化看，也可以接到你自己的脚本/Agent 里。

### 8. 设置定时刷新（关键！）

微信持续产生新消息，你需要定期刷新：

```bash
# 增量解密（检测 WAL 变化，通常 <1 秒）
python3 /path/to/scripts/refresh_decrypt.py --config config.yaml

# 增量同步到 collector.db
python3 /path/to/scripts/collector.py --config config.yaml --sync
```

可以用 crontab、launchd、或 AI Agent 的定时任务来自动跑。

---

## CLI 参考

### refresh_decrypt.py — 增量解密

```bash
python3 refresh_decrypt.py --config config.yaml          # 检测 WAL 变化，patch 新页面
python3 refresh_decrypt.py --config config.yaml --full    # 强制全量解密
```

| 退出码 | 含义 |
|--------|------|
| 0 | 正常 |
| 2 | 密钥验证失败（微信重启过，需重新提取密钥） |

### collector.py — 消息同步

```bash
python3 collector.py --config config.yaml --sync                         # 同步所有会话
python3 collector.py --config config.yaml --sync --chatroom 123@chatroom # 同步单个群
python3 collector.py --config config.yaml --discover                     # 手动发现新会话
```

### extract_todos.py — 私聊待办

```bash
python3 extract_todos.py --config config.yaml         # 增量（最近 35 分钟）
python3 extract_todos.py --config config.yaml --full   # 全量（昨天整天）
```

### extract_calendar.py — 日程扫描

```bash
python3 extract_calendar.py --config config.yaml         # 增量
python3 extract_calendar.py --config config.yaml --full   # 全量
```

### extract_digest.py — 群聊干货

```bash
python3 extract_digest.py --config config.yaml                                    # 默认所有监控群，昨天
python3 extract_digest.py --config config.yaml --groups "123@chatroom,456@chatroom"
python3 extract_digest.py --config config.yaml --date 2026-03-12
```

---

## 文件结构

```
scripts/
  decrypt/
    find_all_keys_macos.c    — 从微信进程内存提取 per-DB 加密密钥（macOS，C）
    decrypt_db.py             — 全量解密所有加密数据库
    config.py                 — YAML 配置加载器
  refresh_decrypt.py          — 增量解密（WAL patch，定时任务用这个）
  collector.py                — 消息增量同步到 collector.db
  extract_todos.py            — 从私聊提取待办对话 → JSON
  extract_calendar.py         — 从私聊+工作群提取日程 → JSON
  extract_digest.py           — 从群聊提取消息 → JSON
  requirements.txt            — Python 依赖
prompts/                      — AI Agent prompt 模板（OpenClaw 适配）
config.example.yaml           — 配置模板
SKILL.md                      — OpenClaw Skill 定义（Agent 集成用）
```

---

## 已知限制 & 待解决

这是个半成品，以下问题已知但尚未解决：

### 🔴 阻塞性问题

- [ ] **微信重启后密钥失效** — 每次微信重启都需要重新 `sudo find_all_keys_macos`。尚无自动化方案（需要 sudo 权限）
- [ ] **macOS Only** — 密钥提取工具目前只支持 macOS。Windows 版需要不同的内存扫描方式（参考 [bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt) 的 `find_all_keys.py`）
- [ ] **需要 Full Disk Access 或 sudo** — 读微信数据库目录需要权限

### 🟡 需要验证

- [ ] **refresh_decrypt.py 未在真实环境验证** — WAL patch 逻辑移植自 bbingz/wechat-decrypt 的 monitor_web.py，在 mock 数据上测试通过，但未在真实 19GB 微信数据上跑过
- [ ] **collector.py 大量历史消息性能** — 首次同步时如果有几十万条消息，性能未测
- [ ] **zstd 解压兼容性** — 微信消息内容使用 zstandard 压缩，不同版本可能有差异

### 🟢 改进方向

- [ ] Windows 支持（密钥提取 + 路径适配）
- [ ] Docker 化（一键部署）
- [ ] MCP Server 集成（让 Claude / ChatGPT 直接查微信消息）
- [ ] Web UI（浏览器查看 collector.db 内容）
- [ ] 更多 extract 脚本（群成员活跃度分析、关键词监控...）
- [ ] 消息解密 streaming（类似 monitor_web.py 的实时推送模式）

---

## 技术细节

### 微信 4.0 加密方案

- **加密**: SQLCipher 4, AES-256-CBC + HMAC-SHA512
- **KDF**: PBKDF2-HMAC-SHA512, 256,000 iterations
- **页面**: 4096 bytes, reserve = 80 (IV 16 + HMAC 64)
- **每个 DB 独立 salt 和 enc_key**
- **密钥位置**: WCDB 缓存 raw key 在进程内存中，格式 `x'<64hex_enc_key><32hex_salt>'`

### WAL 增量解密原理

SQLite WAL 模式下，新写入先进 `.db-wal` 文件。微信的 WAL 是预分配固定大小（4MB），不能用文件大小检测变化，只能用 **mtime**。

WAL 文件包含多个 frame（每个 frame = 24B header + 4096B 加密页面）。同一个 WAL 文件中可能混有上一轮遗留的旧 frame，通过 **WAL header 中的 salt 值**区分：只有 salt 匹配当前周期的 frame 才是有效的。

`refresh_decrypt.py` 做的事：
1. 检查每个加密 DB 的 `.db` 和 `.db-wal` 的 mtime
2. 与上次刷新的 mtime 对比
3. 主 DB 变了 → 全量解密（WAL checkpoint 发生过）
4. 只有 WAL 变了 → 解密 WAL 中的有效 frame，patch 到已解密的 DB 文件
5. 都没变 → 跳过

一个 4MB WAL 的解密和 patch 大约 70ms。

---

## 致谢

核心解密逻辑基于 [bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt/tree/feat/macos-support)，包括：
- 进程内存密钥提取（`find_all_keys_macos.c`）
- SQLCipher 4 页面解密算法
- WAL 增量解密和 salt 校验机制

在此基础上，我们增加了：
- 消息增量同步层（`collector.py`）
- 结构化数据提取（`extract_*.py` 系列）
- AI Agent 集成层（prompt 模板 + OpenClaw Skill）
- HMAC 密钥验证（防止密钥过期时静默产出坏数据）
- DB 过滤（只解密需要的 message/contact/session，跳过 media/emoticon 等）

---

## 参与贡献

这是个半成品，最需要帮助的方向：

1. **在你的机器上跑一遍** — 报告遇到的问题（不同微信版本、不同 macOS 版本）
2. **Windows 适配** — 密钥提取和路径处理
3. **新的 extract 脚本** — 你想从微信聊天里提取什么？
4. **其他 Agent 适配** — Claude Code MCP、ChatGPT Plugin、Dify...
5. **性能优化** — 大数据量下的同步和提取性能

提 Issue 描述问题，或直接 PR。代码风格不强求，能跑就行。

---

## License

MIT
