#!/usr/bin/env python3
"""
collector.py — 微信消息采集转存（一次性同步命令）
逻辑：读 decrypted/ 下已解密的 message DB → 增量转存到 collector.db
用法：
  python3 collector.py --config config.yaml --sync                    # 同步所有 watched_chats
  python3 collector.py --config config.yaml --sync --chatroom ID      # 同步单个群
"""
import os, sys, json, time, glob, sqlite3, hashlib, re, argparse
import xml.etree.ElementTree as _ET
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════
# 配置加载（复用 decrypt/config.py）
# ═══════════════════════════════════════════════════════════
def _load_config(path):
    """加载配置，复用 decrypt/config.py 的 load_config"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config
    return load_config(path)


def parse_args():
    parser = argparse.ArgumentParser(description='微信消息采集器')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--sync', action='store_true', help='执行一次增量同步后退出')
    parser.add_argument('--chatroom', help='只同步指定的 chatroom_id')
    parser.add_argument('--discover', action='store_true', help='扫描解密DB，自动发现并注册所有群/私聊到 watched_chats')
    return parser.parse_args()


# ═══════════════════════════════════════════════════════════
# 全局变量（main 入口初始化）
# ═══════════════════════════════════════════════════════════
COLLECTOR_DB = ''
CONTACT_DB = ''
MSG_DIR = ''
SELF_WXID = ''


# ═══════════════════════════════════════════════════════════
# 联系人名称缓存
# ═══════════════════════════════════════════════════════════
_names = {}


def load_names():
    global _names
    try:
        with sqlite3.connect(CONTACT_DB) as conn:
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            for r in conn.execute("SELECT username, nick_name, remark FROM contact WHERE username != ''"):
                u, n, rk = r
                _names[u] = (rk or '').strip() or (n or '').strip() or u
        print(f'[names] {len(_names)} loaded')
    except Exception as e:
        print(f'[names] {e}')


def get_name(uid):
    return _names.get(uid, uid)


# ═══════════════════════════════════════════════════════════
# 解压 zstd 内容
# ═══════════════════════════════════════════════════════════
try:
    import zstandard as zstd
    _dctx = zstd.ZstdDecompressor()

    def decomp(data):
        try:
            if isinstance(data, (bytes, bytearray)) and len(data) > 4:
                return _dctx.decompress(data, max_output_size=1048576).decode('utf-8', errors='replace')
        except Exception:
            pass
        return data.decode('utf-8', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data or '')
except ImportError:
    def decomp(data):
        return data.decode('utf-8', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data or '')


# ═══════════════════════════════════════════════════════════
# collector.db 初始化
# ═══════════════════════════════════════════════════════════
def init_db():
    with sqlite3.connect(COLLECTOR_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chatroom_id TEXT NOT NULL,
                sender      TEXT,
                content     TEXT,
                msg_time    INTEGER,
                local_id    TEXT,
                msg_type    INTEGER DEFAULT 1,
                UNIQUE(chatroom_id, local_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_time ON messages(chatroom_id, msg_time DESC);
            CREATE TABLE IF NOT EXISTS watched_chats (
                chatroom_id   TEXT PRIMARY KEY,
                chatroom_name TEXT,
                added_at      INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                chatroom_id   TEXT PRIMARY KEY,
                last_local_id TEXT DEFAULT '0',
                last_sync_at  INTEGER DEFAULT 0
            );
        """)
    print(f'[db] {COLLECTOR_DB}')


# ═══════════════════════════════════════════════════════════
# 找消息表（在 decrypted/message/*.db 里搜）
# ═══════════════════════════════════════════════════════════
_table_cache = {}
_table_cache_ts = {}
_TABLE_CACHE_TTL = 300


def find_msg_table(chatroom_id):
    now = time.time()
    cached = _table_cache.get(chatroom_id)
    if cached and cached[0] and now - _table_cache_ts.get(chatroom_id, 0) < _TABLE_CACHE_TTL:
        return cached
    table = f"Msg_{hashlib.md5(chatroom_id.encode()).hexdigest()}"
    best_db = None
    best_ts = -1
    # 扫描解密目录下的 message DB
    # refresh_decrypt.py 输出到 decrypted/message/
    # 兼容旧版 _monitor_cache/ 目录（如果存在）
    dec_root = os.path.dirname(MSG_DIR)  # decrypted/
    scan_dirs = [MSG_DIR]
    cache_dir = os.path.join(dec_root, '_monitor_cache')
    if os.path.isdir(cache_dir) and cache_dir != MSG_DIR:
        scan_dirs.append(cache_dir)
    all_dbs = []
    for d in scan_dirs:
        all_dbs.extend(glob.glob(os.path.join(d, '*.db')))
    for db_path in all_dbs:
        if db_path.endswith('-wal') or db_path.endswith('-shm'):
            continue
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            try:
                found = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if found:
                    # 动态检测时间列名（WeChat DB 可能用 create_time 或 CreateTime）
                    _cols = [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()]
                    _time_col = next((c for c in ['create_time', 'CreateTime', 'msg_time'] if c in _cols), None)
                    if _time_col:
                        max_ts = conn.execute(f"SELECT MAX([{_time_col}]) FROM [{table}]").fetchone()
                        ts = (max_ts[0] or 0) if max_ts else 0
                    else:
                        ts = 0
                    if ts > best_ts:
                        best_ts = ts
                        best_db = db_path
            finally:
                conn.close()
        except Exception:
            pass
    if best_db:
        _table_cache[chatroom_id] = (best_db, table)
        _table_cache_ts[chatroom_id] = now
        return best_db, table
    return None, None


# ═══════════════════════════════════════════════════════════
# Name2Id: real_sender_id → wxid 映射
# ═══════════════════════════════════════════════════════════
_n2id_cache = {}
_n2id_cache_ts = {}


def _load_name2id(db_path):
    now = time.time()
    if db_path in _n2id_cache and now - _n2id_cache_ts.get(db_path, 0) < _TABLE_CACHE_TTL:
        return _n2id_cache[db_path]
    mapping = {}
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        try:
            has_n2id = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'"
            ).fetchone()
            if has_n2id:
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    mapping[row[0]] = row[1]
        finally:
            conn.close()
    except Exception as e:
        print(f'[n2id] load failed {os.path.basename(db_path)}: {e}')
    _n2id_cache[db_path] = mapping
    _n2id_cache_ts[db_path] = now
    return mapping


# ═══════════════════════════════════════════════════════════
# 增量同步单个群
# ═══════════════════════════════════════════════════════════
def sync_one(chatroom_id, last_local_id='0'):
    db_path, table = find_msg_table(chatroom_id)
    if not db_path:
        return 0, last_local_id

    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            id_col = next((c for c in ['local_id', 'MsgLocalID', 'rowid'] if c in cols), 'rowid')
            time_col = next((c for c in ['create_time', 'msg_time', 'CreateTime'] if c in cols), None)
            sender_col = next((c for c in ['real_sender_id', 'sender', 'StrTalker'] if c in cols), None)
            content_col = next((c for c in ['message_content', 'compress_content', 'content', 'Content'] if c in cols), None)
            type_col = next((c for c in ['local_type', 'MsgType', 'Type'] if c in cols), None)
            status_col = next((c for c in ['status'] if c in cols), None)
            if not content_col:
                return 0, last_local_id
            sel = (
                f"SELECT {id_col},{sender_col or 'NULL'},{content_col},{time_col or '0'},"
                f"{type_col or '1'},{status_col or '0'} "
                f"FROM {table} WHERE CAST({id_col} AS INTEGER) > CAST(? AS INTEGER) "
                f"ORDER BY CAST({id_col} AS INTEGER) ASC LIMIT 2000"
            )
            rows = conn.execute(sel, (str(last_local_id),)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f'[sync] {chatroom_id}: {e}')
        return 0, last_local_id

    name2id = _load_name2id(db_path)
    if not rows:
        return 0, last_local_id

    inserted = 0
    new_lid = last_local_id
    coll = sqlite3.connect(COLLECTOR_DB, timeout=30)
    try:
        coll.execute("PRAGMA journal_mode=WAL")
        coll.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass

    try:
        coll.execute("BEGIN")
        is_dm = '@chatroom' not in chatroom_id and '@im.chatroom' not in chatroom_id
        for row in rows:
            lid, _raw_sender, content_raw, msg_time, msg_type = row[0], row[1], row[2], row[3], row[4]

            # 解压内容
            if isinstance(content_raw, (bytes, bytearray)):
                raw_text = decomp(content_raw)
            else:
                raw_text = str(content_raw or '')

            # 检测二进制乱码
            _bad = raw_text.count('\ufffd')
            _ctrl = sum(1 for c in raw_text[:100] if ord(c) < 32 and c not in '\n\r\t')
            if len(raw_text) > 5 and (_bad / max(len(raw_text), 1) > 0.08 or _ctrl > 5):
                _TYPE_MAP = {
                    1: '[📝 文本]', 3: '[🖼️ 图片]', 34: '[🎤 语音]', 43: '[🎥 视频]',
                    47: '[😄 表情]', 49: '[📎 文件/链接]', 10000: '[💬 系统消息]',
                    10002: '[📋 合并转发]'
                }
                mt = int(msg_type or 1)
                content = _TYPE_MAP.get(mt, f'[📎 消息类型 {mt}]')
                if is_dm and _raw_sender:
                    try:
                        _sid = int(_raw_sender)
                        _rwxid = name2id.get(_sid, '')
                        if _rwxid == SELF_WXID:
                            sender = '__self__'
                        elif _rwxid:
                            sender = get_name(_rwxid)
                        else:
                            sender = get_name(chatroom_id)
                    except (ValueError, TypeError):
                        sender = get_name(str(_raw_sender))
                else:
                    sender = get_name(str(_raw_sender or '')) if _raw_sender else ''
            else:
                if '\n' in raw_text:
                    parts = raw_text.split('\n', 1)
                    sender_id = parts[0].strip().rstrip(':')
                    content = parts[1].strip()
                else:
                    sender_id = str(_raw_sender or '')
                    content = raw_text
                sender = get_name(sender_id) if sender_id else ''
                if is_dm:
                    try:
                        sender_int = int(_raw_sender) if _raw_sender else None
                    except (ValueError, TypeError):
                        sender_int = None
                    resolved_wxid = name2id.get(sender_int, '') if sender_int else ''
                    if resolved_wxid == SELF_WXID:
                        sender = '__self__'
                    elif resolved_wxid:
                        sender = get_name(resolved_wxid)
                    elif not sender or sender == str(_raw_sender or ''):
                        sender = get_name(chatroom_id)

            # 修复 sender 被设成 XML 声明的问题
            if sender and (sender.startswith('<?xml') or sender.startswith('<msg')):
                sender = '__self__' if is_dm else get_name(chatroom_id)
            # 纯数字 sender → Name2Id 解析
            if sender and sender.isdigit() and name2id:
                try:
                    wxid = name2id.get(int(sender), '')
                    if wxid == SELF_WXID:
                        sender = '__self__'
                    elif wxid:
                        sender = get_name(wxid)
                    elif is_dm:
                        sender = get_name(chatroom_id)
                except Exception:
                    pass

            content = content[:2000]
            lid_str = str(lid)
            try:
                coll.execute(
                    "INSERT OR IGNORE INTO messages(chatroom_id,sender,content,msg_time,local_id,msg_type) VALUES(?,?,?,?,?,?)",
                    (chatroom_id, sender, content, int(msg_time or 0), lid_str, int(msg_type or 1))
                )
                changed = coll.execute("SELECT changes()").fetchone()[0]
                if changed:
                    inserted += 1
                    new_lid = lid_str
                else:
                    exists = coll.execute(
                        "SELECT 1 FROM messages WHERE chatroom_id=? AND local_id=?",
                        (chatroom_id, lid_str)
                    ).fetchone()
                    if exists:
                        new_lid = lid_str
                    else:
                        print(f"[sync] write skipped {chatroom_id}:{lid_str}, stopping batch")
                        break
            except sqlite3.Error as e:
                print(f'[sync] write failed {chatroom_id}:{lid_str}: {e}')
                break
    finally:
        try:
            coll.commit()
        except Exception:
            new_lid = last_local_id
        try:
            coll.close()
        except Exception:
            pass
    return inserted, new_lid


# ═══════════════════════════════════════════════════════════
# 公众号 / 系统号过滤
# ═══════════════════════════════════════════════════════════
_FILTER_IDS = {
    'brandservicesessionholder', 'brandsessionholder', 'notifymessage',
    'floatbottle', 'fmessage', 'weixin', 'qqmail', 'qmessage', 'tmessage',
    'medianote', 'voipnotify', 'voipmsg', 'weixiread', 'wxid_exporter',
    'mphelper', 'newsapp',
}


def _is_spam(uid):
    if not uid:
        return True
    if uid.startswith('@'):
        return True
    if uid.startswith('gh_'):
        return True
    if uid in _FILTER_IDS:
        return True
    return False


# ═══════════════════════════════════════════════════════════
# 自动发现所有群/私聊
# ═══════════════════════════════════════════════════════════
def discover_chatrooms():
    """扫描 session.db 和 contact.db，发现所有 chatroom_id 并注册到 watched_chats。"""
    discovered = set()

    # 从 session.db 获取所有会话
    dec_root = os.path.dirname(MSG_DIR)
    session_db = os.path.join(dec_root, 'session', 'session.db')
    if os.path.exists(session_db):
        try:
            conn = sqlite3.connect(f'file:{session_db}?mode=ro', uri=True)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            # session 表通常有 username 字段
            for tbl in ['session', 'Session']:
                try:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
                    name_col = next((c for c in ['userName', 'username', 'strUsrName'] if c in cols), None)
                    if name_col:
                        rows = conn.execute(f"SELECT {name_col} FROM {tbl}").fetchall()
                        for (uid,) in rows:
                            if uid and not _is_spam(uid):
                                discovered.add(uid)
                        break
                except Exception:
                    continue
            conn.close()
        except Exception as e:
            print(f'[discover] session.db 读取失败: {e}')

    # 也从 contact.db 补充私聊（有消息表的联系人）
    if os.path.exists(CONTACT_DB):
        try:
            conn = sqlite3.connect(f'file:{CONTACT_DB}?mode=ro', uri=True)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            rows = conn.execute(
                "SELECT username FROM contact WHERE username != '' AND username NOT LIKE 'gh_%'"
            ).fetchall()
            conn.close()
            total_contacts = len(rows)
            checked = 0
            for (uid,) in rows:
                if uid and not _is_spam(uid) and uid not in discovered:
                    db_path, _ = find_msg_table(uid)
                    if db_path:
                        discovered.add(uid)
                checked += 1
                if checked % 100 == 0:
                    print(f'[discover] 联系人扫描进度: {checked}/{total_contacts}')
        except Exception as e:
            print(f'[discover] contact.db 读取失败: {e}')

    if not discovered:
        print('[discover] 未发现任何会话，请检查解密是否成功')
        return 0

    # 注册到 watched_chats
    added = 0
    with sqlite3.connect(COLLECTOR_DB, timeout=30) as conn:
        for cid in discovered:
            name = get_name(cid)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO watched_chats(chatroom_id, chatroom_name) VALUES(?, ?)",
                    (cid, name)
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    added += 1
            except Exception:
                pass
        conn.commit()

    total = len(discovered)
    groups = sum(1 for c in discovered if '@chatroom' in c)
    dms = total - groups
    print(f'[discover] 发现 {total} 个会话（{groups} 群 + {dms} 私聊），新注册 {added} 个')
    return added


# ═══════════════════════════════════════════════════════════
# 主同步逻辑
# ═══════════════════════════════════════════════════════════
def run_sync(chatroom_filter=None, auto_discover=True):
    """执行一次增量同步。chatroom_filter 非空则只同步该群。
    auto_discover=True 时，如果 watched_chats 为空自动执行发现。
    """
    with sqlite3.connect(COLLECTOR_DB) as conn:
        watched = conn.execute("SELECT chatroom_id FROM watched_chats").fetchall()

    # 首次运行自动发现
    if not watched and auto_discover and not chatroom_filter:
        print('[sync] watched_chats 为空，自动发现会话...')
        discover_chatrooms()
        with sqlite3.connect(COLLECTOR_DB) as conn:
            watched = conn.execute("SELECT chatroom_id FROM watched_chats").fetchall()

    if not watched:
        print('[sync] 没有要同步的会话。请先运行 --discover 或手动添加。')
        return 0

    with sqlite3.connect(COLLECTOR_DB) as conn:
        states = dict(conn.execute("SELECT chatroom_id, last_local_id FROM sync_state").fetchall())

    total_inserted = 0
    for (cid,) in watched:
        if chatroom_filter and cid != chatroom_filter:
            continue
        last = states.get(cid, '0')
        n, new_lid = sync_one(cid, last)
        if n > 0 or new_lid != last:
            with sqlite3.connect(COLLECTOR_DB, timeout=30) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sync_state(chatroom_id,last_local_id,last_sync_at) VALUES(?,?,strftime('%s','now'))",
                    (cid, new_lid)
                )
            if n > 0:
                print(f'[sync] {get_name(cid)}: +{n}')
                total_inserted += n

    print(f'[sync] 完成，共写入 {total_inserted} 条新消息')
    return total_inserted


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    args = parse_args()

    if not args.sync and not args.discover:
        print("请使用 --sync 或 --discover 参数", file=sys.stderr)
        sys.exit(1)

    cfg = _load_config(args.config)

    COLLECTOR_DB = cfg['collector_db']
    SELF_WXID = cfg.get('self_wxid', '')
    dec = cfg['decrypted_dir']
    MSG_DIR = os.path.join(dec, 'message')  # decrypt_db.py 输出目录
    CONTACT_DB = os.path.join(dec, 'contact', 'contact.db')

    init_db()
    load_names()

    if args.discover:
        discover_chatrooms()

    if args.sync:
        run_sync(chatroom_filter=args.chatroom)
