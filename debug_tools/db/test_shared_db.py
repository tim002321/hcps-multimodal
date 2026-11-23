#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证"摄像头帧和 BLE 传感器数据写在同一个会话库"。

不开硬件：用假摄像头产帧；心电数据直接写几包到同一个库。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'AAAwindows_code')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from devices.camera_receiver import CameraService          # noqa: E402
from core.config import DATA_DIR                      # noqa: E402
from core.db_storage import DataRecorder, SensorDB    # noqa: E402
from core import media_paths as media                 # noqa: E402
from ui.replay import load_packets                # noqa: E402


class FakeCapture:
    def __init__(self, index=0, backend=0):
        import cv2

        self._opened = True
        self._cv2 = cv2
        self._frames = 0

    def isOpened(self):                               # noqa: N802
        return self._opened

    def set(self, prop, value):                       # noqa: N802
        return True

    def read(self):                                   # noqa: N802
        time.sleep(1 / 60.0)
        cv2 = self._cv2
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, :, 1] = np.linspace(20, 120, 640, dtype=np.uint8)[None, :]
        self._frames += 1
        cv2.putText(frame, 'SHARED DB {}'.format(self._frames), (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (240, 240, 240), 2)
        return True, frame

    def release(self):                                # noqa: N802
        self._opened = False


def main() -> int:
    db_path = os.path.join(DATA_DIR, 'shared_test_{}.db'.format(
        datetime.now().strftime('%H%M%S')))
    storage = SensorDB(db_name=os.path.basename(db_path), data_dir=os.path.dirname(db_path))
    recorder = DataRecorder(storage)

    # 心电：写 3 包（每包 80 点）
    for packet in range(3):
        recorder.record({
            'ECG': {'体温': 36.5, 'ECG': [100 + i for i in range(80)]},
            '呼吸': [0] * 8,
            '三轴': [{'x': 0.0, 'y': 0.0, 'z': 1.0}] * 16,
            '_包号': packet + 1,
        })
    storage.commit()

    service = CameraService({'index': 0, 'fps': 15.0, 'width': 640, 'height': 480,
                             'quality': 80}, db_path=db_path,
                            capture_factory=lambda i, b: FakeCapture(i, b))
    service.start()
    time.sleep(1.5)
    service.stop()
    storage.close()

    conn = sqlite3.connect('file:{}?mode=ro'.format(db_path), uri=True)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {}
    for table in ('ecg_data', 'ecg_frames', 'camera_frames', 'camera_videos'):
        if table in tables:
            counts[table] = conn.execute('SELECT COUNT(*) FROM "{}"'.format(table)).fetchone()[0]
    # 照片落文件后：验库里只存文件名、且每个文件名都能在 frames/ 里找到
    names = []
    if 'camera_frames' in tables:
        names = [r[0] for r in conn.execute(
            'SELECT file_name FROM camera_frames WHERE file_name IS NOT NULL')]
    conn.close()
    print('库：' + db_path)
    print('表：' + ', '.join(sorted(tables)))
    print('行数：' + str(counts))

    frames_path = media.frames_dir(db_path)
    on_disk = [p for p in os.listdir(frames_path)] if os.path.isdir(frames_path) else []
    missing = [n for n in names if not os.path.exists(os.path.join(frames_path, n))]
    print('照片目录：{} 个文件，库内文件名 {} 个，缺失 {}'.format(
        len(on_disk), len(names), len(missing)))

    video_ok = False
    if 'camera_videos' in tables:
        conn2 = sqlite3.connect('file:{}?mode=ro'.format(db_path), uri=True)
        try:
            row = conn2.execute('SELECT file_name, frame_count FROM camera_videos').fetchone()
        finally:
            conn2.close()
        if row and row[0]:
            vpath = media.video_path(db_path, row[0])
            video_ok = os.path.exists(vpath) and int(row[1] or 0) > 0
            print('视频：{} 帧 {}'.format(row[0], row[1]))

    # 回放合并：两类数据按绝对时间排序后应当交替出现
    packets = load_packets(db_path)
    kinds = {}
    for _ts, kind, _decoded in packets:
        kinds[kind] = kinds.get(kind, 0) + 1
    ordered = all(packets[i][0] <= packets[i + 1][0] for i in range(len(packets) - 1))
    print('回放合并：{}，时间轴有序={}'.format(kinds, ordered))
    for ts, kind, _decoded in packets[:6]:
        print('  {:.3f}  {}'.format(ts, kind))
    ok = (counts.get('ecg_data', 0) > 0
          and counts.get('camera_frames', 0) > 0
          and bool(names) and not missing and len(on_disk) >= len(names)
          and video_ok
          and kinds.get('ecg', 0) > 0 and kinds.get('camera', 0) > 0 and ordered)
    print('\n[{}] 传感器数据与照片/视频落同一会话目录，且回放时间轴可合并'.format(
        '通过' if ok else '失败'))

    # 自己的产物自己收走：照片/视频目录和 -wal 会留一堆，下一轮跑测试就分不清是新是旧
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    root = media.media_root(db_path)
    if os.path.isdir(root):
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    print('测试库与媒体目录已清理')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
