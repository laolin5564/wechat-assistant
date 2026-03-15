# 待办扫描 — Cron agentTurn Prompt

## 任务

从微信私聊中提取待办事项，有变化则推送到 Discord。

## 执行步骤

### 1. 刷新解密 + 同步消息

> 如果 `{{ssh_host}}` 非空，所有 python3 命令需要通过 SSH 执行：
> `sshpass -p '{{ssh_password}}' ssh -o StrictHostKeyChecking=no {{ssh_host}} "cd {{skill_dir}}/scripts && python3 ..."`
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

### 2. 提取私聊数据

```bash
python3 extract_todos.py --config {{config_path}}
```

> 输出 JSON 到 stdout，包含 `conversations` 和 `existing_todos`。

### 3. 分析 JSON 输出

从 conversations 中识别待办事项。

#### 什么算待办
- 对方**请求我做的事**（明确的 action item）
- **我承诺要做的事**（"好的我去处理"、"我来搞"）
- 涉及**金钱、合同、法律**的事项（urgent=true）
- 有**明确 deadline** 的事项（urgent=true）

#### 什么不算待办
- 纯聊天、寒暄、问好
- 已经当场解决的问题
- 咨询性质的对话（我在回答别人问题）
- 广告、推销、群发消息
- 纯表情、图片消息

#### 去重规则
- 检查 `existing_todos` 中是否已存在相似待办（同一联系人 + 相似 summary）
- 已存在的不重复添加
- 检查是否有待办在对话中被解决（resolved）

### 4. 更新 todos.json

todos.json 路径：从 config.yaml 的 `state.todos_file` 读取（默认在 config.yaml 同级目录的 `./todos.json`）。

用 `exec` 工具执行 Python 脚本读写：

```bash
python3 -c "
import json, os
path = '{{config_path}}'.replace('config.yaml', '') + 'todos.json'
# ... 读取、更新、写回
"
```

或直接用 `read` + `write` 工具操作文件。

更新规则：
- 新增的待办：`status: "open"`，含 `contact`, `summary`, `urgent`, `created`, `forum_thread_id`（初始为 null，发帖后回填）, `last_mentioned`（初始为 created 时间）
- 已解决的：`status: "done"`，加 `resolved_date`

### 5. 推送到 Discord Forum + 闭环追踪

#### 5a. 读取已有帖子列表

```
message(action="thread-list", target="{{forum_id}}")
```

#### 5b. 读取 todos.json 中所有 status="open" 的待办

每条 open 的待办都有 `forum_thread_id` 字段（关联 Forum 帖子）。

#### 5c. 用户手动关帖检测

检查 5a 获取的帖子列表中，是否有 open 待办对应的帖子已被**用户手动关闭（archived）**：
- 如果帖子已 archived 但 todos.json 中还是 `"open"` → 说明用户手动完成了
- todos.json 中 `status` 改为 `"done"`，加 `resolved_date`
- 跳过该待办的后续检查

#### 5d. 对话闭环检查

对每条仍然 open 的待办，检查本次提取的对话中是否有状态变化：

1. **完成信号**（对方说"搞定了""已处理""OK""好的""done"等，或我说"已完成""搞定"等）→
   - 回复原帖：`message(action="thread-reply", threadId=forum_thread_id, message="✅ 已完成 — YYYY-MM-DD")`
   - 关帖（archive）
   - todos.json 中 `status` 改为 `"done"`，加 `resolved_date`
2. **对方催促**（对方再次提到同一件事、催问进度）→
   - 回复原帖：`message(action="thread-reply", threadId=forum_thread_id, message="🔔 对方再次提及（YYYY-MM-DD HH:MM）")`
   - 更新 todos.json 的 `last_mentioned`
3. **超时提醒**（open 超过 7 天且 `last_mentioned` 超过 3 天）→
   - 回复原帖：`message(action="thread-reply", threadId=forum_thread_id, message="⏰ 已 N 天未跟进")`
4. **无变化** → 跳过，不发任何消息

#### 5e. 新增待办发帖

对每条新发现的待办：

- 发帖到 `{{forum_id}}`，`appliedTags=["📋待办"]`
- **帖子标题**：
  - 紧急：`🔴 联系人 — 待办摘要`
  - 跟进：`🟡 联系人 — 待办摘要`
- **帖子内容**：
  ```
  📋 **待办详情**

  **联系人**：XXX
  **摘要**：待办描述
  **紧急程度**：🔴紧急 / 🟡跟进
  **创建时间**：YYYY-MM-DD HH:MM

  💬 **来源对话摘录**
  > 相关对话内容...
  ```
- 拿到返回的 thread_id，写入 todos.json 的 `forum_thread_id` 字段

#### 5f. 发帖方式说明

> 使用 OpenClaw `message` 工具，`action=thread-create`，`target={{forum_id}}`，
> `threadName=帖子标题`，`message=帖子内容`，`appliedTags=["📋待办"]`。
>
> ⚠️ 如果 `message(action=thread-create)` 报错，改用 `exec` 执行 curl：
> ```bash
> curl -s -X POST "https://discord.com/api/v10/channels/{{forum_id}}/threads" \
>   -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"name":"帖子标题","applied_tags":["tag_id"],"message":{"content":"帖子内容"}}'
> ```
> Tag IDs 需要先用 `curl GET /channels/{{forum_id}}` 查 `available_tags` 获取。

无变化则不发任何消息。
