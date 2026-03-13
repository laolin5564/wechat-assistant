# 🦞 wechat-assistant

**OpenClaw Skill** — 微信 AI 个人助手，自动从微信聊天中提取待办、日程、干货，推送到 Discord。

已在多台 Mac 上验证通过（macOS 14/15，微信 4.0+，Python 3.9/3.13）。

---

## 它能干什么

装好后跟 Agent 说 `帮我设置微信助手`，它会引导完成配置。之后自动定时运行：

| 功能 | 频率 | 做什么 |
|------|------|--------|
| 📋 **待办提取** | 每 30 分钟 | 扫描私聊 → AI 识别待办 → 推送 Discord |
| 📅 **日程扫描** | 每 30 分钟 (8-23点) | 扫描私聊+工作群 → AI 识别日程 → 创建 Apple Calendar |
| 📰 **干货收集** | 每天 9:00 | 扫描指定群聊 → AI 提炼干货 → 日报归档 |

### 隐私说明

数据提取（解密、同步、JSON 输出）全程本地运行。AI 分析阶段，提取后的聊天摘要会发送到你配置的大模型 API（Claude、GPT 等）进行分析。原始加密数据库不会离开本机。

---

## 安装

```bash
git clone https://github.com/laolin5564/wechat-assistant.git ~/.openclaw/skills/wechat-assistant
```

然后跟 Agent 说：

> 帮我设置微信助手

Agent 会按 SKILL.md 引导你完成：编译工具 → 提取密钥 → 解密 → 首次同步 → 注册定时任务。

### 环境要求

- **macOS 13+**（ARM64 或 Intel）
- **微信桌面版 4.0+** 正在运行
- **Python 3.8+** + `pip3 install pycryptodome zstandard pyyaml`
- **sudo 权限**（仅密钥提取需要）
- **OpenClaw**（[openclaw.ai](https://openclaw.ai)）

---

## 工作原理

```
┌─────────────────────────────────────┐
│  OpenClaw Agent                      │
│  cron 触发 → 读 prompt 模板          │
│  → exec CLI → 拿 JSON → AI 分析     │
│  → 推送 Discord / 创建日历           │
└────────────┬────────────────────────┘
             │ python3 scripts/...
┌────────────▼────────────────────────┐
│  CLI 工具层（纯 Python，无 AI 依赖） │
│                                      │
│  refresh_decrypt → collector → extract│
│  增量解密 → 同步 → 输出 JSON         │
└─────────────────────────────────────┘
```

**CLI 只做数据提取，AI 分析由 prompt 模板驱动。** 想改分析逻辑？改 `prompts/*.md`，不用动代码。

### 数据流

```
微信进程 → 加密 DB + WAL
    │
    ① refresh_decrypt.py ── 检测 WAL 变化 → 增量 patch
    │
    ② collector.py --sync ── 增量同步到 collector.db
    │
    ③ extract_*.py ── 按场景提取 → JSON 到 stdout
    │
    ④ Agent 读 prompt 模板 → 分析 JSON → 推送结果
```

---

## 增量解密

微信 4.0 用 SQLCipher 4 加密本地数据库。全量解密慢，但 SQLite WAL 模式下新消息写入 `.db-wal` 文件。

`refresh_decrypt.py` 只解密变化的 WAL frame → patch 到已解密的 DB。一个 4MB WAL patch 约 70ms，定时任务几乎无感知。

首次全量解密根据数据量不同，耗时从几十秒到几分钟不等。

### 密钥过期

微信每次重启密钥就变了。`refresh_decrypt.py` 解密前做 HMAC 验证，失败时 exit code 2，Agent 自动提醒重新提取密钥。

---

## 文件结构

```
scripts/
  decrypt/
    find_all_keys_macos.c     ── 从微信进程内存提取密钥（C，需 sudo）
    decrypt_db.py              ── 全量解密（首次用）
    config.py                  ── YAML 配置加载
  refresh_decrypt.py           ── 增量解密（定时用，WAL patch）
  collector.py                 ── 消息同步到 collector.db
  extract_todos.py             ── 私聊待办 → JSON
  extract_calendar.py          ── 日程对话 → JSON
  extract_digest.py            ── 群聊干货 → JSON
prompts/
  todo-scan.md                 ── 待办扫描 prompt 模板
  calendar-scan.md             ── 日程扫描 prompt 模板
  digest.md                    ── 干货收集 prompt 模板
config.example.yaml            ── 配置模板
SKILL.md                       ── Agent 读这个
```

---

## Prompt 模板

`prompts/*.md` 是 cron 的指令模板，注册时替换占位符：

| 占位符 | 含义 |
|--------|------|
| `{{config_path}}` | config.yaml 绝对路径 |
| `{{skill_dir}}` | skill 根目录 |
| `{{thread_id}}` | Discord 子区 ID |
| `{{groups}}` | 监控群 ID（逗号分隔） |
| `{{ssh_host}}` | 微信机器 SSH 地址（本机留空） |

完整列表见 SKILL.md Step 8。

---

## 已知限制

| 级别 | 问题 | 说明 |
|------|------|------|
| 🔴 | 微信重启后密钥失效 | 需重新 `sudo find_all_keys_macos` |
| 🔴 | macOS Only | Windows 需要不同的内存扫描 |
| 🟡 | 首次同步慢 | 历史消息多时几分钟，可 `--chatroom` 分批 |
| 🟡 | DB 结构差异 | 已兼容 `create_time`/`CreateTime`，可能有遗漏 |

---

## 参与共建

1. **跑一遍** — 不同微信版本、macOS 版本的兼容性反馈
2. **新 prompt** — 改变 AI 分析方式
3. **新 extract 脚本** — 输出 JSON 就行，prompt 跟着加
4. **Windows 适配** — 密钥提取 + 路径处理
5. **Bug 修复** — Issue 或 PR，能跑就行

---

## 致谢

核心解密逻辑基于 [bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt/tree/feat/macos-support)（进程内存密钥提取、SQLCipher 4 解密、WAL 增量 patch）。

## License

MIT
