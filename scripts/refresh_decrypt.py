#!/usr/bin/env python3
"""
refresh_decrypt.py — 增量刷新解密数据库（WAL patch 模式）

首次运行：全量解密所有 DB（调 decrypt_db.py 逻辑）
后续运行：只解密变化的 WAL 文件，patch 到已解密的 DB 上（~70ms/DB）

原理：
  微信使用 SQLite WAL 模式，新消息写入 .db-wal 文件（预分配 4MB 固定大小）。
  检测 WAL 的 mtime 变化 → 解密 WAL 中的有效 frame → patch 到已解密的 DB。
  只有 WAL checkpoint 时主 .db 文件才会变，此时需要重新全量解密该 DB。

用法：
  python3 refresh_decrypt.py --config config.yaml          # 增量刷新
  python3 refresh_decrypt.py --config config.yaml --full    # 强制全量解密

依赖：pycryptodome
参考：https://github.com/bbingz/wechat-decrypt (monitor_web.py)
"""
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import sys
import time
import argparse

from Crypto.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

STATE_FILE_NAME = '.refresh_state.json'

# 只解密这些子目录（collector.py 只用 message/ 和 contact/）
NEEDED_PREFIXES = ('message/', 'contact/', 'session/')


def normalize_rel_path(path):
    """规范化相对路径，跨平台匹配"""
    return path.replace("\\", "/").strip("/")


def derive_mac_key(enc_key, salt):
    """从 enc_key 派生 HMAC 密钥"""
    mac_salt = bytes(b ^ 0x3a for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def verify_page1_hmac(db_path, enc_key):
    """验证 Page 1 HMAC，确认密钥有效。

    返回 True=密钥正确, False=密钥错误/文件损坏。
    密钥错误通常意味着微信重启过，需要重新提取密钥。
    """
    with open(db_path, 'rb') as f:
        page1 = f.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        return False

    salt = page1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored_hmac = page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    return hm.digest() == stored_hmac


def decrypt_page(enc_key, page_data, pgno):
    """解密单个加密页面"""
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ))
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def full_decrypt_one(db_path, out_path, enc_key):
    """全量解密一个 DB 文件"""
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    if file_size % PAGE_SZ != 0:
        total_pages += 1

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))

    return total_pages


def patch_wal(wal_path, out_path, enc_key):
    """解密 WAL 中的有效 frame，patch 到已解密的 DB 文件。

    WAL 是预分配固定大小(4MB)，包含当前有效 frame 和上一轮遗留的旧 frame。
    通过 WAL header 中的 salt 值区分：只有 salt 匹配当前周期的 frame 才有效。

    返回 patch 的页数。
    """
    if not os.path.exists(wal_path):
        return 0

    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0

    patched = 0
    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        # 读 WAL header，获取当前 salt
        wal_hdr = wf.read(WAL_HEADER_SZ)
        if len(wal_hdr) < WAL_HEADER_SZ:
            return 0
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]

        frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ  # 24 + 4096

        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]

            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break

            # 校验: pgno 有效 且 salt 匹配当前 WAL 周期
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue  # 旧周期遗留的 frame，跳过

            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1

    return patched


def load_state(state_path):
    """加载上次刷新的 mtime 状态"""
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_state(state_path, state):
    """保存 mtime 状态"""
    os.makedirs(os.path.dirname(state_path) or '.', exist_ok=True)
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2)


def get_mtimes(db_path):
    """获取 .db 和 .db-wal 的 mtime"""
    db_mt = os.path.getmtime(db_path) if os.path.exists(db_path) else 0
    wal_mt = os.path.getmtime(db_path + '-wal') if os.path.exists(db_path + '-wal') else 0
    return db_mt, wal_mt


def main():
    parser = argparse.ArgumentParser(description='增量刷新解密数据库')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--full', action='store_true', help='强制全量解密')
    args = parser.parse_args()

    # 加载配置
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config
    cfg = load_config(args.config)

    db_dir = cfg['db_dir']
    out_dir = cfg['decrypted_dir']
    keys_file = cfg['keys_file']
    state_path = os.path.join(out_dir, STATE_FILE_NAME)

    if not db_dir or not os.path.isdir(db_dir):
        print(f'[ERROR] 微信数据库目录不存在: {db_dir}', file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(keys_file):
        print(f'[ERROR] 密钥文件不存在: {keys_file}', file=sys.stderr)
        print('请先运行 find_all_keys_macos 提取密钥', file=sys.stderr)
        sys.exit(1)

    with open(keys_file) as f:
        raw_keys = json.load(f)
    # 规范化路径匹配（GitHub 原版做法）
    keys = {}
    for path, value in raw_keys.items():
        if path.startswith('_'):
            continue
        keys[normalize_rel_path(path)] = value

    # 收集需要解密的 DB 文件（只处理 message/contact/session）
    db_files = []
    for root, dirs, files in os.walk(db_dir):
        for fn in files:
            if fn.endswith('.db') and not fn.endswith('-wal') and not fn.endswith('-shm'):
                path = os.path.join(root, fn)
                rel = normalize_rel_path(os.path.relpath(path, db_dir))
                if rel not in keys:
                    continue
                # 只解密 collector.py 需要的子目录
                if not any(rel.startswith(p) for p in NEEDED_PREFIXES):
                    continue
                db_files.append((rel, path))

    if not db_files:
        print('[WARN] 没有找到可解密的数据库文件')
        return

    prev_state = {} if args.full else load_state(state_path)
    new_state = {}

    t0 = time.perf_counter()
    full_count = 0
    wal_count = 0
    skip_count = 0
    hmac_fail_count = 0
    total_pages = 0

    for rel, src_path in db_files:
        enc_key = bytes.fromhex(keys[rel]['enc_key'])
        out_path = os.path.join(out_dir, rel)
        wal_path = src_path + '-wal'

        db_mt, wal_mt = get_mtimes(src_path)
        prev = prev_state.get(rel, {})
        prev_db_mt = prev.get('db_mtime', 0)
        prev_wal_mt = prev.get('wal_mtime', 0)

        new_state[rel] = {'db_mtime': db_mt, 'wal_mtime': wal_mt}

        # 判断需要做什么
        need_full = args.full or not os.path.exists(out_path)
        db_changed = db_mt != prev_db_mt
        wal_changed = wal_mt != prev_wal_mt

        if need_full or db_changed:
            # 全量解密前先验证 HMAC（检测密钥是否过期）
            try:
                if not verify_page1_hmac(src_path, enc_key):
                    print(f'[ERROR] HMAC 验证失败 {rel} — 密钥可能已过期（微信重启过？）',
                          file=sys.stderr)
                    hmac_fail_count += 1
                    continue
            except PermissionError:
                print(f'[ERROR] 无权读取 {src_path} — 需要 sudo 或 Full Disk Access',
                      file=sys.stderr)
                hmac_fail_count += 1
                continue

            # 全量解密
            try:
                pages = full_decrypt_one(src_path, out_path, enc_key)
                total_pages += pages
                full_count += 1
                # 同时 patch WAL
                if os.path.exists(wal_path):
                    wp = patch_wal(wal_path, out_path, enc_key)
                    total_pages += wp
            except Exception as e:
                print(f'[ERROR] 全量解密失败 {rel}: {e}')
                continue

        elif wal_changed:
            # WAL patch（主 DB 没变，只有 WAL 更新）
            try:
                wp = patch_wal(wal_path, out_path, enc_key)
                total_pages += wp
                wal_count += 1
            except Exception as e:
                print(f'[ERROR] WAL patch 失败 {rel}: {e}')
                continue
        else:
            skip_count += 1

    elapsed = (time.perf_counter() - t0) * 1000
    save_state(state_path, new_state)

    total = full_count + wal_count + skip_count
    print(
        f'[refresh] {total} 个DB: {full_count} 全量解密, '
        f'{wal_count} WAL patch, {skip_count} 跳过 | '
        f'{total_pages} 页 | {elapsed:.0f}ms'
    )

    if hmac_fail_count > 0:
        print(
            f'[WARN] {hmac_fail_count} 个 DB 密钥验证失败！'
            '请重新运行 find_all_keys_macos 提取密钥',
            file=sys.stderr
        )
        sys.exit(2)  # 特殊退出码，Agent 可据此提醒用户


if __name__ == '__main__':
    main()
