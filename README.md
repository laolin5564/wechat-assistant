<p align="center">
  <img src="assets/banner.png" alt="WX Echo banner" width="800">
</p>

<p align="center">
  <b>OpenClaw Skill</b> — 微信 AI 个人助手，自动从微信聊天中提取待办、日程、干货，推送到 Discord Forum，闭环追踪任务状态。
  <br>念念不忘，必有回响 — Important messages always echo back.
  <br>已在多台 Mac 上验证通过（macOS 14/15，微信 4.0+，Python 3.9/3.13），Windows 基础兼容
</p>

---

## 🦞 龙虾快速开始

复制下面这段话发给你的 Agent（大黄 / Claude / 任何 OpenClaw Agent）：

> 帮我安装 wx-echo skill：先 git clone https://github.com/laolin5564/openclaw-wx-echo 到 ~/.openclaw/skills/wx-echo，然后按 SKILL.md 引导我设置

Agent 会自动帮你：克隆代码 → 编译工具 → 提取密钥 → 解密 → 同步 → 注册定时任务。**全程跟着 Agent 走就行。**

> ⚠️ 需要 macOS + 微信桌面版正在运行 + `pip3 install pycryptodome zstandard pyyaml`

---

## ✨ 功能

<p align="center">
  <img src="assets/features.png" alt="features" width="800">
</p>

装好后跟 Agent 说 `帮我设置微信助手`，自动引导完成配置，之后定时运行。

### 隐私说明

数据提取（解密、同步、JSON 输出）全程本地运行。AI 分析阶段，提取后的聊天摘要会发送到你配置的大模型 API（Claude、GPT 等）。原始加密数据库不会离开本机。

---

## ⚙️ 工作原理

<p align="center">
  <img src="assets/architecture.png" alt="architecture" width="800">
</p>

**CLI 只做数据提取，AI 分析由 prompt 模板驱动。** 想改分析逻辑？改 `prompts/*.md`，不用动代码。

---

## 🚀 安装

```bash
git clone https://github.com/laolin5564/openclaw-wx-echo.git ~/.openclaw/skills/wx-echo
```

然后跟 Agent 说：`帮我设置微信助手`

### 环境要求

- **macOS 13+**（ARM64 或 Intel）
- **微信桌面版 4.0+** 正在运行
- **Python 3.8+** + `pip3 install pycryptodome zstandard pyyaml`
- **sudo 权限**（仅密钥提取需要）
- **[OpenClaw](https://openclaw.ai)**

---

## 🔐 增量解密

微信 4.0 用 SQLCipher 4 加密本地数据库。全量解密慢，但 SQLite WAL 模式下新消息写入 `.db-wal` 文件。

`refresh_decrypt.py` 只解密变化的 WAL frame → patch 到已解密的 DB。一个 4MB WAL patch 约 70ms，定时任务几乎无感知。

微信重启后密钥会变。`refresh_decrypt.py` 解密前做 HMAC 验证，失败时 exit code 2，Agent 自动提醒重新提取密钥。

---

## 📁 文件结构

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

## 📝 Prompt 模板

`prompts/*.md` 是 cron 的指令模板，注册时替换占位符：

| 占位符 | 含义 |
|--------|------|
| `{{config_path}}` | config.yaml 绝对路径 |
| `{{skill_dir}}` | skill 根目录 |
| `{{forum_id}}` | Discord Forum 频道 ID |
| `{{groups}}` | 监控群 ID（逗号分隔） |
| `{{ssh_host}}` | 微信机器 SSH 地址（本机留空） |

完整列表见 SKILL.md Step 8。

---

## ⚠️ 已知限制

| 级别 | 问题 | 说明 |
|------|------|------|
| 🔴 | 微信重启后密钥失效 | 需重新 `sudo find_all_keys_macos` |
| 🟡 | Windows 密钥提取 | 基础编码兼容已完成，密钥提取待适配 |
| 🟡 | 首次同步慢 | 历史消息多时几分钟，可 `--chatroom` 分批 |
| 🟡 | DB 结构差异 | 已兼容多种列名，可能有遗漏 |

---

## 🤝 参与共建

1. **跑一遍** — 不同微信/macOS 版本的兼容性反馈
2. **新 prompt** — 改变 AI 分析方式
3. **新 extract 脚本** — 输出 JSON 就行，prompt 跟着加
4. **Windows 适配** — 密钥提取 + 路径处理
5. **Bug 修复** — Issue 或 PR，能跑就行

---

## 致谢

核心解密逻辑基于 [bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt/tree/feat/macos-support)（进程内存密钥提取、SQLCipher 4 解密、WAL 增量 patch）。

## License

MIT
