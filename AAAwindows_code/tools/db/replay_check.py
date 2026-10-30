#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查一个库回放时会带出哪些数据（心电 / 手环 / 摄像头，含旧库的自动配对）。

用法：
    python tools/replay_check.py                       # 用 data/ 里最新的会话库
    python tools/replay_check.py --db data/20261017_150206.db
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ui.replay import find_camera_companion, load_packets   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='回放数据检查')
    parser.add_argument('--db', default='')
    args = parser.parse_args()

    db_path = args.db
    if not db_path:
        candidates = [p for p in glob.glob(os.path.join(ROOT, 'data', '*.db'))
                      if 'camera_' not in os.path.basename(p)]
        db_path = max(candidates, key=os.path.getmtime)
    print('库：' + db_path)
    companion = find_camera_companion(db_path)
    if companion:
        print('摄像头配对：' + os.path.basename(companion))

    packets = load_packets(db_path)
    kinds = {}
    for _ts, kind, _decoded in packets:
        kinds[kind] = kinds.get(kind, 0) + 1
    ordered = all(packets[i][0] <= packets[i + 1][0] for i in range(len(packets) - 1))
    start = datetime.fromtimestamp(packets[0][0]) if packets else None
    end = datetime.fromtimestamp(packets[-1][0]) if packets else None
    print('条目：{}'.format(kinds))
    print('时间范围：{} ~ {}'.format(start, end))
    print('时间轴有序：{}'.format(ordered))
    for ts, kind, _decoded in packets[:5]:
        print('  {:.3f}  {}'.format(ts, kind))
    if 'ecg' in kinds and 'camera' in kinds and ordered:
        print('\n[通过] 心电与摄像头在同一条回放时间轴上')
        return 0
    print('\n[失败] 缺少心电或摄像头数据')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
