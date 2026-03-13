#!/usr/bin/env python3
"""
extract_calendar.py — 从 collector.db 提取可能含日程的对话，输出 JSON（不调 AI）

扫描私聊 + 工作群消息，过滤噪音后输出结构化 JSON 供 Agent 分析。

用法：
  python3 extract_calendar.py --config config.yaml           # 增量：最近 35 分钟
  python3 extract_calendar.py --config config.yaml --full     # 全量：昨天整天

输出 JSON 到 stdout:
{
  "scan_mode": "full|incremental",
  "conversations": [
    {"chatroom_id": "...", "contact_name": "...", "type": "dm|group", "messages": [...]}
  ],
  ...
}
"""
import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))

# 每段对话保留的最大消息数
MAX_MESSAGES_PER_CONV = 20


def parse_args():
    parser = argparse.ArgumentParser(description='从 collector.db 提取日程相关对话')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--full', action='store_true', help='全量模式：昨天整天')
    return parser.parse_args()


def load_config(config_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config as _load
    return _load(config_path)


def is_noise(chatroom_id, sender):
    """噪音过滤"""
    if chatroom_id.startswith('gh_'):
        return True
    NOISE_IDS = (
        'brandservicesessionholder', 'brandsessionholder',
        'mphelper', 'newsapp', 'weixin', 'floatbottle',
        'fmessage', 'tmessage', 'medianote',
    )
    if chatroom_id in NOISE_IDS:
        return True
    if '@placeholder_' in chatroom_id or '@openim' in chatroom_id:
        return True
    return False


def is_junk_content(content):
    """过滤无意义内容"""
    if not content or len(content.strip()) < 3:
        return True
    if content.startswith('<msg') or content.startswith('<?xml'):
        return True
    junk_prefixes = ('[img:', '[🖼️', '[📎', '<voipbubble', '<voipinvitemsg',
                     '[语音通话]', '[视频通话]')
    if any(content.startswith(p) for p in junk_prefixes):
        return True
    return False


def is_ad_sender(sender_name):
    """检测广告 / 营销类 sender"""
    AD_KEYWORDS = ('京东', '家电', '限时', '政策', '售前售后', '客服',
                   '酒店小助理', '写真馆', '线上写真', '苏宁', '方太',
                   '华帝', '供应', '老板稀缺', '美的', '海尔', '格力',
                   '国美', '五星', '电器')
    if not sender_name:
        return False
    return any(kw in sender_name for kw in AD_KEYWORDS)


def main():
    args = parse_args()
    cfg = load_config(args.config)

    collector_db = cfg['collector_db']
    self_wxid = cfg.get('self_wxid', '')
    work_groups = cfg.get('work_groups', {})

    now = datetime.now(tz=_TZ8)
    now_ts = int(now.timestamp())

    if args.full:
        today_0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today_0 - timedelta(days=1)
        since = int(yesterday.timestamp())
        until = int(today_0.timestamp())
        mode = 'full'
    else:
        since = now_ts - 35 * 60
        until = now_ts
        mode = 'incremental'

    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', 'replace')

    all_rows = conn.execute(
        "SELECT chatroom_id, sender, content, msg_time FROM messages "
        "WHERE msg_time >= ? AND msg_time < ? ORDER BY msg_time ASC",
        (since, until)
    ).fetchall()

    total_messages = len(all_rows)
    noise_count = 0

    conversations = {}

    for chatroom_id, sender, content, msg_time in all_rows:
        if is_noise(chatroom_id, sender):
            noise_count += 1
            continue
        if is_junk_content(content):
            noise_count += 1
            continue
        if is_ad_sender(sender):
            noise_count += 1
            continue

        # 广告内容过滤
        AD_CONTENT_PATTERNS = ('亓', '🔍搜', '👉http', '领券', '红包', '抢券',
                               '限时政策', '拍③件', '预约抽', '立减金')
        if any(p in (content or '') for p in AD_CONTENT_PATTERNS):
            noise_count += 1
            continue

        # 判断对话类型
        is_group = chatroom_id.endswith('@chatroom')
        conv_type = 'group' if is_group else 'dm'

        # 非工作群的群聊跳过
        if is_group and chatroom_id not in work_groups:
            noise_count += 1
            continue

        try:
            dt = datetime.fromtimestamp(msg_time, tz=_TZ8)
            time_str = dt.strftime('%H:%M')
        except Exception:
            time_str = str(msg_time)

        # sender 处理
        if not sender:
            sender_display = '__unknown__'
        elif sender == self_wxid or sender == '__self__':
            sender_display = '__self__'
        else:
            sender_display = sender

        if chatroom_id not in conversations:
            conversations[chatroom_id] = {
                'chatroom_id': chatroom_id,
                'type': conv_type,
                'messages': [],
            }

        conversations[chatroom_id]['messages'].append({
            'sender': sender_display,
            'content': content[:500],
            'time': time_str,
        })

    # 构建输出
    conv_list = []
    for chatroom_id, conv in conversations.items():
        msgs = conv['messages']
        recent_msgs = msgs[-MAX_MESSAGES_PER_CONV:]

        # 过滤全是自己的单向对话
        senders = set(m['sender'] for m in recent_msgs)
        if senders == {'__self__'}:
            noise_count += len(msgs)
            continue

        # 获取联系人名
        is_group = conv['type'] == 'group'
        if chatroom_id in work_groups:
            contact_name = work_groups[chatroom_id]
        else:
            # 从 watched_chats 查或用 chatroom_id
            try:
                row = conn.execute(
                    "SELECT chatroom_name FROM watched_chats WHERE chatroom_id = ?",
                    (chatroom_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                row = None  # watched_chats 表可能不存在
            if row and row[0]:
                contact_name = row[0]
            elif not is_group:
                # 私聊：用对方 sender 名
                for m in recent_msgs:
                    if m['sender'] != '__self__' and m['sender'] != '__unknown__':
                        contact_name = m['sender']
                        break
                else:
                    contact_name = chatroom_id
            else:
                contact_name = chatroom_id

        conv_list.append({
            'chatroom_id': chatroom_id,
            'contact_name': contact_name,
            'type': conv['type'],
            'messages': recent_msgs,
        })

    conn.close()

    # 按消息数降序
    conv_list.sort(key=lambda c: len(c['messages']), reverse=True)

    output = {
        'scan_time': now_ts,
        'scanned_since': since,
        'scanned_until': until,
        'scan_mode': mode,
        'conversations': conv_list,
        'total_messages': total_messages,
        'conversations_count': len(conv_list),
        'filtered_noise': noise_count,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
