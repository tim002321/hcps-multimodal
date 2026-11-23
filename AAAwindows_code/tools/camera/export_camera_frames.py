#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把摄像头库里录的帧导出成图片，方便离线做表情/动作分析。

用法：
    python tools/export_camera_frames.py data/camera_20261017_143135.db
    python tools/export_camera_frames.py data/camera_xxx.db --out frames --every 30
    python tools/export_camera_frames.py data/camera_xxx.db --index 120

默认导出全部帧到 `<库名>_frames/`；--every 30 表示每 30 帧取 1 帧（1fps）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description='导出摄像头数据库里的帧')
    parser.add_argument('db', help='camera_*.db')
    parser.add_argument('--out', default='', help='输出目录（默认 <库名>_frames）')
    parser.add_argument('--every', type=int, default=1, help='每 N 帧导一张')
    parser.add_argument('--index', type=int, default=0, help='只导某一帧（0 表示全部）')
    parser.add_argument('--limit', type=int, default=0, help='最多导多少张（0 = 不限）')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f'找不到数据库：{args.db}')
        return 1
    out_dir = Path(args.out or (os.path.splitext(args.db)[0] + '_frames'))
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info("camera_frames")')}
        # 新格式库里 jpeg 是空的，图片在 file 指向的文件夹里（相对库所在目录）
        tail = ', file' if 'file' in columns else ''
        if args.index:
            rows = conn.execute(
                f'SELECT frame_index, time, jpeg{tail} FROM camera_frames WHERE frame_index=?',
                (args.index,)).fetchall()
        else:
            rows = conn.execute(
                f'SELECT frame_index, time, jpeg{tail} FROM camera_frames ORDER BY frame_index'
            ).fetchall()
    finally:
        conn.close()

    base = os.path.dirname(os.path.abspath(args.db))
    written = 0
    for position, row in enumerate(rows):
        frame_index, stamp, jpeg = row[0], row[1], row[2]
        if not jpeg and len(row) > 3 and row[3]:
            rel = row[3]
            path = rel if os.path.isabs(rel) else os.path.join(base, *rel.split('/'))
            try:
                with open(path, 'rb') as handle:
                    jpeg = handle.read()
            except OSError:
                continue
        if not jpeg:
            continue
        if args.index == 0 and position % max(1, args.every):
            continue
        if args.limit and written >= args.limit:
            break
        name = out_dir / f'frame_{int(frame_index):06d}.jpg'
        name.write_bytes(jpeg)
        written += 1
    print(f'导出 {written} 张 -> {out_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
