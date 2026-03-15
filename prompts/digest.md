# 干货收集 — Cron agentTurn Prompt

## 任务

从微信监控群中提炼昨天的干货内容，推送到 Discord Forum。

## 执行步骤

### 1. 刷新解密 + 同步消息

> 如果 `{{ssh_host}}` 非空，所有 python3 命令需要通过 SSH 执行：
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

### 2. 提取群聊数据

```bash
python3 extract_digest.py --config {{config_path}} --groups "{{groups}}" --date yesterday
```

> 输出 JSON 到 stdout，包含每个群的消息列表。

### 3. 分析 JSON 输出

从每个群的消息中提炼干货。

#### 什么是干货
- **工具 / 产品推荐**（含链接或具体名称）
- **技术方案 / 经验分享**（代码、架构、方法论）
- **行业洞察 / 趋势分析**
- **有价值的资源链接**（教程、文档、开源项目）
- **实战案例 / 踩坑记录**
- **重要新闻 / 政策变化**

#### 什么是噪音
- 日常闲聊、灌水
- 广告、推销
- 重复的接龙、回复
- 纯表情、贴图
- 已被大量转发的陈旧信息
- 拉票、投票、砍价类

### 4. 推送到 Discord Forum

发帖到 `{{forum_id}}`，`appliedTags=["📰干货"]`。

- **帖子标题**：`📰 YYYY-MM-DD 群聊精华`
- **帖子内容**：

```
📰 **YYYY-MM-DD 微信群干货日报**

---

### 🔥 精选 Top 3

1. **标题/主题** — 一句话总结
   > 关键内容摘录（50-100字）
   📌 来源：群名 · 发送者 · HH:MM
   🔗 相关链接（如有）

2. ...

3. ...

---

### 📂 按群分组

#### 群名1（N 条有效 / M 条总计）
- 🔹 **主题** — 摘要（发送者 HH:MM）
- 🔹 **主题** — 摘要（发送者 HH:MM）

#### 群名2（N / M）
- ...

---
📊 统计：X 个群 · Y 条干货 · Z 条过滤
```

如果所有群都没有干货，不发任何消息。

#### 发帖方式说明

> 使用 OpenClaw `message` 工具，`action=thread-create`，`target={{forum_id}}`，
> `threadName=帖子标题`，`message=帖子内容`，`appliedTags=["📰干货"]`。
>
> ⚠️ 如果 `message(action=thread-create)` 报错，改用 `exec` 执行 curl：
> ```bash
> curl -s -X POST "https://discord.com/api/v10/channels/{{forum_id}}/threads" \
>   -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"name":"帖子标题","applied_tags":["tag_id"],"message":{"content":"帖子内容"}}'
> ```
> Tag IDs 需要先用 `curl GET /channels/{{forum_id}}` 查 `available_tags` 获取。
