#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出 data/ 下每个库都有哪些数据（心电 / 手环 / 摄像头），用来排查"数据是不是分开存了"。

用法：
    python tools/db_overview.py
    python tools/db_overview.py --limit 20
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
DATA_DIR = os.path.join(ROOT, 'data')

from core import media_paths as media                      # noqa: E402
from ui.replay import format_size                          # noqa: E402


def count(conn, table):
    try:
        return conn.execute('SELECT COUNT(*) FROM "{}"'.format(table)).fetchone()[0]
    except sqlite3.Error:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='数据库内容总览')
    parser.add_argument('--limit', type=int, default=15)
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(DATA_DIR, '*.db')),
                   key=os.path.getmtime, reverse=True)[:max(1, args.limit)]
    if not paths:
        print('data/ 下没有数据库')
        return 1
    print('{:<30} {:<12} {:>6} {:>7} {:>7} {:>9}'.format(
        '库文件', '修改时间', '照片', '心电', '手环PPG', '照片+视频'))
    print('-' * 78)
    for path in paths:
        try:
            conn = sqlite3.connect('file:{}?mode=ro'.format(path), uri=True)
        except sqlite3.Error as exc:
            print('{:<30} 打不开：{}'.format(os.path.basename(path), exc))
            continue
        try:
            camera = count(conn, 'camera_frames')
            ecg = count(conn, 'ecg_data')
            ppg = count(conn, 'bracelet_ppg_data')
        finally:
            conn.close()
        stamp = time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(path)))
        # 照片/视频不在库里，单独统计目录体积
        extra = format_size(media.media_bytes(path))
        print('{:<30} {:<12} {:>6} {:>7} {:>7} {:>9}'.format(
            os.path.basename(path), stamp, camera, ecg, ppg, extra))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
