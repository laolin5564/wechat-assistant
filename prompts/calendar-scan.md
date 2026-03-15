# 日程扫描 — Cron agentTurn Prompt

## 任务

从微信私聊和工作群中扫描日程信息，创建 Apple Calendar 事件，推送到 Discord。

## 执行步骤

### 1. 刷新解密 + 同步消息

> 如果 `{{ssh_host}}` 非空，所有 python3/osascript 命令需要通过 SSH 执行：
> `sshpass -p '{{ssh_password}}' ssh -o StrictHostKeyChecking=no {{ssh_host}} "command"`
> 如果为空，直接本地执行。

```bash
cd {{skill_dir}}/scripts

# 增量解密（WAL patch，通常 <1 秒）
# 如果退出码=2，表示密钥过期（微信重启过），发告警后终止
python3 refresh_decrypt.py --config {{config_path}}

# 同步到 collector.db
python3 collector.py --config {{config_path}} --sync
```

> **如果 `refresh_decrypt.py` 输出包含 "HMAC 验证失败" 或退出码为 2：**
> 发 Discord 告警：`⚠️ 微信密钥已过期，需要重新提取。请运行 sudo find_all_keys_macos`
> 然后**终止本次任务**，不继续后续步骤。

### 2. 提取日程数据

```bash
python3 extract_calendar.py --config {{config_path}}
```

> 输出 JSON 到 stdout，包含按对话分组的消息。

### 3. 分析 JSON 输出

从 conversations 中识别日程事件。

#### 什么算日程
- **明确的时间 + 地点/事件**（"周五下午3点开会"、"明天10点到公司"）
- **约见面 / 约饭 / 约会议**（含具体时间）
- **截止日期提醒**（"月底前交材料"）
- **航班 / 高铁 / 出行安排**

#### 什么不算日程
- 模糊的"改天聊"、"有空见"
- 过去的事件（已经发生的）
- 别人的日程（跟老林无关）
- 纯讨论未确认的计划

#### 需要老林确认参与的
- 聚餐邀约、活动邀请 → 创建事件但标注"待确认"
- 工作群里安排的会议 → 直接创建

### 4. 创建日历事件

> ⚠️ **osascript 需要 macOS GUI 会话**。如果 OpenClaw 跑在无头 Mac（如 Mac Mini Server），
> osascript 会阻塞。此时必须通过 SSH 到有 GUI 会话的机器执行（通常是用户的 MacBook）。
> 如果 `{{ssh_host}}` 非空，用 SSH 执行 osascript。如果为空且当前机器无 GUI，跳过日历创建，
> 只推送 Discord 提醒。

对每个确认的日程，用 osascript 创建：

```bash
osascript -e '
tell application "Calendar"
  tell calendar "日历"
    make new event with properties {summary:"事件标题", start date:date "2026-03-15 15:00:00", end date:date "2026-03-15 16:00:00", description:"来源：微信 - 联系人名"}
  end tell
end tell'
```

### 5. 推送到 Discord Forum

#### 5a. 读取已有帖子列表

```
message(action="thread-list", target="{{forum_id}}")
```

#### 5b. 过期检查

检查已有帖子中带 📅日程 tag 的，如果事件时间已过：
- 回复原帖：`message(action="thread-reply", threadId=原帖ID, message="✅ 日程已过")`
- 关帖（archive）

#### 5c. 去重检查

按**事件标题 + 时间**匹配，避免重复创建。

#### 5d. 新日程发帖

对每条新日程，发帖到 `{{forum_id}}`，`appliedTags=["📅日程"]`：

- **帖子标题**：
  - 已确认：`📌 事件标题 — 3月15日 15:00`
  - 待确认：`🤔 事件标题 — 待确认`
- **帖子内容**：
  ```
  📅 **日程详情**

  **事件**：事件标题
  **时间**：YYYY-MM-DD HH:MM — HH:MM
  **地点**：XXX（如有）
  **状态**：📌已确认 / 🤔待确认
  **来源联系人**：XXX

  💬 **对话摘录**
  > 相关对话内容...

  🗓️ 已添加到 Apple Calendar（如已创建）
  ```

无日程则不发消息。

#### 5e. 发帖方式说明

> 使用 OpenClaw `message` 工具，`action=thread-create`，`target={{forum_id}}`，
> `threadName=帖子标题`，`message=帖子内容`，`appliedTags=["📅日程"]`。
>
> ⚠️ 如果 `message(action=thread-create)` 报错，改用 `exec` 执行 curl：
> ```bash
> curl -s -X POST "https://discord.com/api/v10/channels/{{forum_id}}/threads" \
>   -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"name":"帖子标题","applied_tags":["tag_id"],"message":{"content":"帖子内容"}}'
> ```
> Tag IDs 需要先用 `curl GET /channels/{{forum_id}}` 查 `available_tags` 获取。
