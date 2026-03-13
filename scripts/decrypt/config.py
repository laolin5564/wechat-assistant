"""
配置加载器 — 从 YAML 配置文件加载路径配置
支持 --config 参数指定配置文件路径
"""
import os
import sys
import yaml


def load_config(config_path=None):
    """加载 YAML 配置文件，返回扁平 dict。

    Args:
        config_path: YAML 配置文件路径。如果为 None，尝试从 sys.argv 解析 --config。

    Returns:
        dict: 包含所有配置项的扁平字典
    """
    if config_path is None:
        # 从命令行参数查找 --config
        for i, arg in enumerate(sys.argv):
            if arg == '--config' and i + 1 < len(sys.argv):
                config_path = sys.argv[i + 1]
                break
        if config_path is None:
            # 默认查找脚本所在目录的上两级（skill 根目录）
            skill_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            config_path = os.path.join(skill_dir, 'config.yaml')

    if not os.path.exists(config_path):
        print(f"[ERROR] 配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    base = os.path.dirname(os.path.abspath(config_path))

    def _abs(p):
        """将相对路径转为基于配置文件目录的绝对路径"""
        if p and not os.path.isabs(p):
            return os.path.join(base, p)
        return p or ''

    wechat = raw.get('wechat', {})
    monitor = raw.get('monitor', {})
    state = raw.get('state', {})

    cfg = {
        # 微信
        'db_dir': os.path.expanduser(wechat.get('db_dir', '')),
        'self_wxid': wechat.get('self_wxid', ''),
        'decrypted_dir': _abs(wechat.get('decrypted_dir', './decrypted')),
        'collector_db': _abs(wechat.get('collector_db', './collector.db')),
        'keys_file': _abs(wechat.get('keys_file', './all_keys.json')),

        # 派生路径
        'msg_cache_dir': '',
        'contact_db': '',
        'session_db': '',

        # 监控
        'monitor_groups': monitor.get('groups', []),
        'work_groups': monitor.get('work_groups', {}),

        # 状态文件
        'todos_file': _abs(state.get('todos_file', './todos.json')),
        'todo_state_file': _abs(state.get('todo_state_file', './todo_state.json')),
        'calendar_state_file': _abs(state.get('calendar_state_file', './calendar_sync_state.json')),
    }

    # 派生默认路径
    dec = cfg['decrypted_dir']
    cfg['msg_cache_dir'] = os.path.join(dec, '_monitor_cache')
    cfg['contact_db'] = os.path.join(dec, 'contact', 'contact.db')
    cfg['session_db'] = os.path.join(dec, 'session', 'session.db')

    return cfg
