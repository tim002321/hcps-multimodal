# -*- coding: utf-8 -*-
"""端到端冒烟测试：采集 -> 分发 -> 落库 -> 回读，全过程不需要硬件。

用法：
    python tools/smoke_test.py --seconds 5                 # 模拟源
    python tools/smoke_test.py --seconds 30 --real          # 按 config/default.yaml 起真实采集器
    python tools/smoke_test.py --db D:\\tmp\\t.db            # 指定库文件
    python tools/smoke_test.py --fresh                      # 强制新开一个会话目录，不复用旧的

会话目录默认按「同名就复用」的老规矩来；如果复用到的库是别的会话建的、当前身份写不了，
会自动换一个带时间戳的会话名，不再直接报 readonly。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A0_data_collect import CollectorManager          # noqa: E402
from core.A1_data_store import SqliteStore, usable_session_db_path    # noqa: E402


def load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit('缺少 PyYAML：pip install pyyaml')
    with open(path, encoding='utf-8') as fp:
        return yaml.safe_load(fp) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description='HcpsApp 采集链路冒烟测试')
    parser.add_argument('--config', default=str(ROOT / 'config' / 'default.yaml'))
    parser.add_argument('--seconds', type=float, default=5.0, help='运行时长')
    parser.add_argument('--user', default='smoke', help='会话用户名（建库目录用）')
    parser.add_argument('--task', default='selftest', help='会话任务名')
    parser.add_argument('--root', default=None, help='会话根目录，默认读配置里的 session.root')
    parser.add_argument('--db', default=None, help='直接指定数据库文件，覆盖会话目录规则')
    parser.add_argument('--fresh', action='store_true', help='强制新开一个会话目录，不复用旧的')
    parser.add_argument('--real', action='store_true', help='按配置启动真实采集器（不注入模拟源）')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
    )

    config = load_config(Path(args.config))
    session = config.get('session') or {}

    if args.db:
        db_path = args.db
    else:
        db_path = usable_session_db_path(
            user=args.user,
            task=args.task,
            root=args.root or session.get('root', '实验结果'),
            result_dir=session.get('result_dir', 'normal_result'),
            db_name=session.get('db_name', 'database.db'),
            fresh=args.fresh,
        )

    store = SqliteStore(db_path).start()
    manager = CollectorManager.from_config(config, use_sim=not args.real)
    manager.add_sink(store.submit)
    manager.start()

    print(f'\n数据库：{db_path}')
    print(f'采集器：{", ".join(manager.collectors) or "（无）"}')
    print(f'运行 {args.seconds:.0f} 秒…\n')

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(1.0)
            line = ' | '.join(
                f'{name}: {info["packets"]}包/{info["state"]}' for name, info in manager.status().items()
            )
            print(f'  {line}')
    except KeyboardInterrupt:
        print('\n手动中断…')
    finally:
        manager.stop()
        time.sleep(0.3)          # 让分发队列排空
        store.stop()

    print('\n=== 结果 ===')
    for name, info in (manager.status() or {}).items():
        print(f'采集器 {name:10s} 状态={info["state"]:12s} 包数={info["packets"]:6d} '
              f'错误={info["errors"]} 重连={info["reconnects"]} {info["last_error"]}')
    print(f'入库条数：{store.written}')
    counts = store.counts()
    for table, n in counts.items():
        if n:
            print(f'  {table:12s} {n}')

    total = sum(n for n in counts.values() if n > 0)
    if total <= 0:
        print('\n[失败] 一行数据都没落库，检查采集器是否真的收到数据')
        return 1
    print('\n[通过] 采集 -> 分发 -> 落库链路正常')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
