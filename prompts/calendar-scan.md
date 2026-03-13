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

### 4. 创建 Apple Calendar 事件

对每个确认的日程，用 osascript 创建：

```bash
osascript -e '
tell application "Calendar"
  tell calendar "日历"
    make new event with properties {summary:"事件标题", start date:date "2026-03-15 15:00:00", end date:date "2026-03-15 16:00:00", description:"来源：微信 - 联系人名"}
  end tell
end tell'
```

### 5. 推送到 Discord

发送到 thread {{thread_id}}：

格式：
```
📅 **YYYY-MM-DD HH:MM 日程扫描**

✅ **已创建日程**
1. 📌 **事件标题** — 3月15日 15:00-16:00
   来源：联系人名 · 已添加到 Apple Calendar

⏳ **待确认**
1. 🤔 **聚餐邀约** — 周六晚上
   来源：张三 · 需要你确认是否参加

如果没有日程，不发消息。
```
