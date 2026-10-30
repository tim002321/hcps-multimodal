# -*- coding: utf-8 -*-
"""视频流水线测试（不需要硬件）：
dry_run 造帧 -> A0 发现新帧并发包 -> A1 落 data_video_frame + 帧文件在会话目录里

用法：
    python tools/test_video_pipeline.py --seconds 3
    python tools/test_video_pipeline.py --fresh          # 强制新开一个会话目录

断言按「本次跑出来的增量」算，所以复用旧会话目录（老帧还在）重跑也是绿的。
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A0_data_collect import CollectorManager      # noqa: E402
from core.A0_data_collect.video import VideoCollector          # noqa: E402
from core.A1_data_store import FileStore, SqliteStore, usable_session_dirs   # noqa: E402


def count_rows(db_path: str) -> int:
    """库里 data_video_frame 已有多少行（库或表还不存在就是 0）。"""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute('SELECT COUNT(*) FROM data_video_frame').fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='视频采集+落库流水线测试')
    parser.add_argument('--seconds', type=float, default=3.0)
    parser.add_argument('--task', default='videopipe')
    parser.add_argument('--fresh', action='store_true', help='强制新开一个会话目录，不复用旧的')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    dirs = usable_session_dirs(user='smoke', task=args.task, root=str(ROOT / '实验结果'), fresh=args.fresh)
    files = FileStore(dirs['result_dir'])
    frames_dir = files.dir('video1')
    rows_before = count_rows(dirs['db_path'])
    files_before = set(glob.glob(os.path.join(frames_dir, '*.jpg')))

    store = SqliteStore(dirs['db_path']).start()
    manager = CollectorManager()
    manager.register(VideoCollector({
        'session_dir': dirs['result_dir'],
        'out_dir': 'video1',
        'frame_pattern': '1_pic_%06d.jpg',
        'poll_interval': 0.2,
        'dry_run': True,
    }))
    manager.add_sink(store.submit)
    manager.start()

    print(f'\n会话目录：{dirs["result_dir"]}')
    print(f'帧目录：{frames_dir}')
    print(f'采集 {args.seconds:.0f} 秒（dry_run），开始前库里已有 {rows_before} 行…')
    time.sleep(args.seconds)
    manager.stop()
    time.sleep(0.3)
    store.stop()

    conn = sqlite3.connect(dirs['db_path'])
    rows = conn.execute('SELECT COUNT(*) FROM data_video_frame').fetchone()[0]
    sample = conn.execute('SELECT camera, frame_index, path FROM data_video_frame ORDER BY id LIMIT 1').fetchone()
    conn.close()
    rows_added = rows - rows_before
    new_files = set(glob.glob(os.path.join(frames_dir, '*.jpg'))) - files_before
    files_on_disk = len(glob.glob(os.path.join(frames_dir, '*.jpg')))
    stats = list(manager.status().values())[0]

    print(f'\n采集器：发包 {stats["packets"]} 个，状态 {stats["state"]}')
    print(f'帧文件：本次新增 {len(new_files)} 个（目录里共 {files_on_disk} 个）')
    print(f'data_video_frame：本次新增 {rows_added} 行（共 {rows} 行）')
    if sample:
        print(f'  样例：camera={sample[0]} index={sample[1]} path=...{str(sample[2])[-38:]}')

    ok = stats['packets'] > 0 and rows_added == stats['packets'] and len(new_files) >= stats['packets']
    print('\n[通过] 视频帧 -> 发包 -> 落库 流水线正常' if ok else '\n[失败] 帧数与落库行数不一致')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
