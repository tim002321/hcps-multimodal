#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把摄像头库里录的帧导出成图片，方便离线做表情/动作分析。

用法：
    python debug_tools/camera/export_camera_frames.py data/camera_20261017_143135.db
    python debug_tools/camera/export_camera_frames.py data/<会话>.db --out my_frames --every 15
    python debug_tools/camera/export_camera_frames.py data/<会话>.db --index 120

两种库都支持：
  * 新库（照片存文件）：库里只有文件名，这里直接从 frames/ 复制文件，不读进内存；
  * 老库（jpeg BLOB）：从库里读字节写出来。
默认输出到 `<库名>_frames_export/`（刻意避开会话目录下真正的 frames/，
免得把人家的原始照片目录覆盖掉）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'AAAwindows_code')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ui.replay import camera_frame_refs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='导出摄像头数据库里的帧')
    parser.add_argument('db', help='会话库（含 camera_frames 表）')
    parser.add_argument('--out', default='', help='输出目录（默认 <库名>_frames_export）')
    parser.add_argument('--every', type=int, default=1, help='每 N 帧导一张')
    parser.add_argument('--index', type=int, default=0, help='只导某一帧（0 表示全部）')
    parser.add_argument('--limit', type=int, default=0, help='最多导多少张（0 = 不限）')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f'找不到数据库：{args.db}')
        return 1

    refs = camera_frame_refs(args.db)
    if not refs:
        print('这个库里没有摄像头帧（camera_frames 为空或不存在）')
        return 1

    out_dir = Path(args.out or (os.path.splitext(args.db)[0] + '_frames_export'))
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    missing = 0
    for position, ref in enumerate(refs):
        if args.index:
            if int(ref['frame_index']) != args.index:
                continue
        elif position % max(1, args.every):
            continue
        if args.limit and written >= args.limit:
            break

        name = out_dir / f'frame_{int(ref["frame_index"]):06d}.jpg'
        src = ref.get('jpeg_path')
        if src:
            if not os.path.exists(src):
                missing += 1
                continue
            shutil.copyfile(src, name)
        else:
            jpeg = ref.get('jpeg') or b''
            if not jpeg:
                missing += 1
                continue
            name.write_bytes(jpeg)
        written += 1

    print(f'导出 {written} 张 -> {out_dir}')
    if missing:
        print(f'有 {missing} 帧文件缺失，已跳过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
