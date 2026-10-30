#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把旧版本单独存的 camera_*.db 合并进会话库（追加 camera_frames，不删原文件）。

旧版本（2026-10-17 之前）摄像头帧单独存在 `data/camera_YYYYmmdd_HHMMSS.db`；
新版本已经直接写进会话库。这个工具用于把历史数据也物理合并到一起。

用法：
    python tools/merge_camera_db.py --session data/20261017_150206.db
    python tools/merge_camera_db.py --session data/xxx.db --camera data/camera_xxx.db

不指定 --camera 时按文件名时间自动配对最接近的一个。已存在的帧（time+frame_index 相同）
会跳过，所以重复执行是安全的。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from devices.camera_receiver import DDL                     # noqa: E402
from ui.replay import find_camera_companion, load_camera_rows   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='把 camera_*.db 合并进会话库')
    parser.add_argument('--session', required=True, help='会话库（心电/手环所在的 data/xxxx.db）')
    parser.add_argument('--camera', default='', help='摄像头库（默认自动配对）')
    args = parser.parse_args()

    session_db = args.session
    if not os.path.exists(session_db):
        print('找不到会话库：' + session_db)
        return 1
    camera_db = args.camera or find_camera_companion(session_db)
    if not camera_db or not os.path.exists(camera_db):
        print('没有找到可配对的摄像头库')
        return 1

    rows = load_camera_rows(camera_db)
    if not rows:
        print('摄像头库里没有帧：' + camera_db)
        return 1
    print('会话库：{}'.format(session_db))
    print('摄像头库：{}（{} 帧）'.format(camera_db, len(rows)))

    conn = sqlite3.connect(session_db)
    try:
        for ddl in DDL:
            conn.execute(ddl)
        existing = {(r[0], r[1]) for r in conn.execute(
            'SELECT time, frame_index FROM camera_frames')}
        copied = 0
        for stamp, frame_index, jpeg in rows:
            if (stamp, frame_index) in existing:
                continue
            conn.execute('INSERT INTO camera_frames (time, camera, frame_index, width, height, jpeg)'
                         ' VALUES (?,?,?,?,?,?)',
                         (stamp, 'usb0', frame_index, 640, 480, jpeg))
            copied += 1
        conn.commit()
        total = conn.execute('SELECT COUNT(*) FROM camera_frames').fetchone()[0]
    finally:
        conn.close()
    print('新增 {} 帧，合并后会话库共 {} 帧'.format(copied, total))
    print('[完成] 可以用 tools/replay_check.py 验证统一回放')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
