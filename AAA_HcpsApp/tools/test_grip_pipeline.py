# -*- coding: utf-8 -*-
"""握力流水线测试（不需要硬件）：
模拟原始帧 -> A0 落 data_grip_raw -> A2 加工 -> 老格式 data_grip_l / data_grip_r

用法：
    python tools/test_grip_pipeline.py --seconds 3
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A0_data_collect import CollectorManager      # noqa: E402
from core.A0_data_collect.sim import SimCollector              # noqa: E402
from core.A1_data_store import SqliteStore                      # noqa: E402
from core.A2_analyze import process_raw_table              # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='握力采集+加工流水线测试')
    parser.add_argument('--seconds', type=float, default=3.0)
    parser.add_argument('--db', default=None)
    parser.add_argument('--task', default='grippipe')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    db_path = args.db or SqliteStore.session_db_path(user='smoke', task=args.task, root=str(ROOT / '实验结果'))
    store = SqliteStore(db_path).start()

    manager = CollectorManager()
    manager.register(SimCollector({'kind': 'grip', 'name': 'sim_grip', 'rate_hz': 10, 'device': 'grip'}))
    manager.add_sink(store.submit)
    manager.start()

    print(f'\n数据库：{db_path}')
    print(f'采集 {args.seconds:.0f} 秒原始帧…')
    time.sleep(args.seconds)
    manager.stop()
    time.sleep(0.3)
    store.stop()

    conn = sqlite3.connect(db_path)
    raw_count = conn.execute('SELECT COUNT(*) FROM data_grip_raw').fetchone()[0]
    print(f'\n原始帧入库：data_grip_raw = {raw_count}')

    print('开始 A2 加工…')
    result = process_raw_table(db_path)
    print(f"加工结果：读取 {result['raw']} 条，写入 左手 {result['L']} / 右手 {result['R']}")

    for table in ('data_grip_l', 'data_grip_r'):
        row = conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
        if row:
            print(f'  {table} 样例：time={row[1]}  angle={row[2]}  press={str(row[3])[:60]}...')
    conn.close()

    ok = raw_count > 0 and (result['L'] + result['R']) > 0
    print('\n[通过] 原始帧 -> 加工 -> 老格式表 流水线正常' if ok else '\n[失败] 流水线没有产出')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
