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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT, 'data')


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
    print('{:<34} {:<12} {:>7} {:>8} {:>8}'.format('库文件', '修改时间', '摄像头', '心电', '手环PPG'))
    print('-' * 78)
    for path in paths:
        try:
            conn = sqlite3.connect('file:{}?mode=ro'.format(path), uri=True)
        except sqlite3.Error as exc:
            print('{:<34} 打不开：{}'.format(os.path.basename(path), exc))
            continue
        try:
            camera = count(conn, 'camera_frames')
            ecg = count(conn, 'ecg_data')
            ppg = count(conn, 'bracelet_ppg_data')
        finally:
            conn.close()
        stamp = time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(path)))
        print('{:<34} {:<12} {:>7} {:>8} {:>8}'.format(
            os.path.basename(path), stamp, camera, ecg, ppg))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
