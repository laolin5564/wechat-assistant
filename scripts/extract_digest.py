#!/usr/bin/env python3
"""
extract_digest.py — 从 collector.db 提取群聊消息，输出 JSON（不调 AI）

用法：
  python3 extract_digest.py --config config.yaml                                 # 默认：config 中所有 monitor.groups，昨天
  python3 extract_digest.py --config config.yaml --groups "123@chatroom,456@chatroom"
  python3 extract_digest.py --config config.yaml --date yesterday
  python3 extract_digest.py --config config.yaml --date 2026-03-12

输出 JSON 到 stdout:
{
  "date": "2026-03-12",
  "groups": [
    {"id": "...", "name": "...", "total": 100, "filtered": 80, "messages": [...]}
  ]
}
"""
import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))


def parse_args():
    parser = argparse.ArgumentParser(description='从 collector.db 提取群聊消息')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--groups', help='群 ID 列表，逗号分隔（默认用 config 中的 monitor.groups）')
    parser.add_argument('--date', default='yesterday', help='日期: yesterday 或 YYYY-MM-DD')
    return parser.parse_args()


def load_config(config_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config as _load
    return _load(config_path)


def get_group_name(conn, group_id, names_cache):
    """获取群名"""
    if group_id in names_cache:
        return names_cache[group_id]
    try:
        row = conn.execute(
            "SELECT chatroom_name FROM watched_chats WHERE chatroom_id = ?",
            (group_id,)
        ).fetchone()
        name = row[0] if row and row[0] else group_id
    except sqlite3.OperationalError:
        name = group_id  # watched_chats 表可能不存在
    names_cache[group_id] = name
    return name


def main():
    args = parse_args()
    cfg = load_config(args.config)

    collector_db = cfg['collector_db']

    # 确定群列表
    if args.groups:
        group_ids = [g.strip() for g in args.groups.split(',') if g.strip()]
    else:
        group_ids = cfg.get('monitor_groups', [])

    if not group_ids:
        print(json.dumps({'error': '未指定群 ID，请用 --groups 参数或在 config.yaml 的 monitor.groups 配置'}))
        sys.exit(1)

    # 确定日期范围
    now = datetime.now(tz=_TZ8)
    if args.date == 'yesterday':
        d = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    else:
        try:
            d = datetime.strptime(args.date, '%Y-%m-%d').replace(tzinfo=_TZ8)
        except ValueError:
            print(json.dumps({'error': f'日期格式错误: {args.date}，请用 YYYY-MM-DD'}))
            sys.exit(1)

    ts_start = int(d.timestamp())
    ts_end = ts_start + 86400
    date_label = d.strftime('%Y-%m-%d')

    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', 'replace')

    names_cache = {}
    result_groups = []

    for gid in group_ids:
        rows = conn.execute(
            """SELECT sender, content, msg_time FROM messages
               WHERE chatroom_id=? AND msg_time >= ? AND msg_time < ?
               AND msg_type NOT IN (3, 47)
               ORDER BY msg_time""",
            (gid, ts_start, ts_end)
        ).fetchall()

        filtered = []
        for sender, content, ts in rows:
            if not content or len(content) < 5:
                continue
            if sender == '__self__':
                continue
            if content.startswith('[img:') or content.startswith('[🖼️'):
                continue
            if content.startswith('<?xml') or content.startswith('<msg'):
                continue
            if content.startswith('[📎 消息类型'):
                continue
            # 纯表情过滤
            stripped = content.replace(' ', '')
            if all(c in '[]' or ord(c) > 0x1F600 for c in stripped if not c.isspace()):
                continue
            filtered.append({
                'sender': sender,
                'content': content[:500],
                'time': datetime.fromtimestamp(ts, _TZ8).strftime('%H:%M')
            })

        # 消息过多时只保留有实质内容的
        if len(filtered) > 300:
            filtered = [m for m in filtered if len(m['content']) > 20][:300]

        name = get_group_name(conn, gid, names_cache)
        result_groups.append({
            'id': gid,
            'name': name,
            'total': len(rows),
            'filtered': len(filtered),
            'messages': filtered,
        })

    conn.close()

    output = {
        'date': date_label,
        'groups': result_groups,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
