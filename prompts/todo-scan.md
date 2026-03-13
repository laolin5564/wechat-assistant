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

读取当前 `{{config_path}}` 对应的 `state.todos_file`，更新后写回：
- 新增的待办：`status: "open"`，含 `contact`, `summary`, `urgent`, `created`
- 已解决的：`status: "done"`，加 `resolved_date`

### 5. 推送到 Discord

**只在有变化时**（新增或完成）发送到 thread {{thread_id}}：

格式：
```
📋 **YYYY-MM-DD HH:MM 微信待办更新**

🔴 **紧急**
1. **联系人** — 待办描述

🟡 **需跟进**
1. **联系人** — 待办描述（创建日期）

✅ **已完成**
- ~~联系人 — 待办描述~~

📊 N 新增 · N 完成 · N 待处理
```

如果没有变化，不发消息。
