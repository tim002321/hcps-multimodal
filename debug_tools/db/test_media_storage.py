#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「照片落文件 + 视频单独存」这套存储是否正确（纯离线，不开真摄像头）。

    python debug_tools/db/test_media_storage.py

检查项：
  1. camera_frames 只有元数据，没有 jpeg 列；有 file_name 列
  2. frames/ 里的文件数 == 表行数，且每行的 file_name 都能在磁盘上找到
  3. 每个文件是合法 JPEG（文件头 FF D8）
  4. camera_videos 落了一行，mp4 能被 cv2 打开、帧数 > 0
  5. 老库（jpeg BLOB）仍能被 load_camera_rows 读出来
  6. 删掉一帧文件后，load_packets 不抛异常（回放容错）
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'AAAwindows_code')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import media_paths as media                     # noqa: E402
from devices.camera_receiver import CameraService         # noqa: E402
from ui.replay import camera_columns, camera_frame_refs, load_camera_rows, load_packets  # noqa: E402


class FakeCapture:
    """按 60fps 出合成帧，让节拍丢弃逻辑把入库稳定在目标帧率。"""

    def __init__(self, index=0, backend=0):
        self._opened = True
        self._frames = 0

    def isOpened(self):                               # noqa: N802
        return self._opened

    def set(self, prop, value):                       # noqa: N802
        return True

    def read(self):                                   # noqa: N802
        import cv2

        time.sleep(1 / 60.0)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, :, 1] = np.linspace(20, 120, 640, dtype=np.uint8)[None, :]
        self._frames += 1
        cv2.putText(frame, 'MEDIA {}'.format(self._frames), (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (240, 240, 240), 2)
        return True, frame

    def release(self):                                # noqa: N802
        self._opened = False


results = []


def check(name, ok, detail=''):
    results.append((name, ok))
    print('  [{}] {}{}'.format('通过' if ok else '失败', name,
                               '  ({})'.format(detail) if detail else ''))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix='media_test_')
    db_path = os.path.join(tmp, '20261114_120000.db')
    print('临时库：' + db_path)

    print('\n[1] 采集 1.6 秒（假摄像头 60fps → 入库 15fps）')
    service = CameraService({'index': 0, 'fps': 15.0, 'width': 640, 'height': 480,
                             'quality': 80}, db_path=db_path,
                            capture_factory=lambda i, b: FakeCapture(i, b))
    service.start()
    time.sleep(1.6)
    stats = service.stats()
    service.stop()
    time.sleep(0.3)
    print('  stats: 入库 {} 帧, db={:.0f}KB, media={:.0f}KB, video_ok={}'.format(
        stats['written'], stats['db_size'] / 1024, stats['media_bytes'] / 1024,
        stats['video_ok']))

    print('\n[2] 表结构')
    conn = sqlite3.connect('file:{}?mode=ro'.format(db_path), uri=True)
    cols = camera_columns(conn)
    row_count = conn.execute('SELECT COUNT(*) FROM camera_frames').fetchone()[0]
    names = [r[0] for r in conn.execute('SELECT file_name FROM camera_frames')]
    vrow = conn.execute('SELECT file_name, frame_count FROM camera_videos').fetchone() \
        if 'camera_videos' in [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")] else None
    conn.close()
    check('camera_frames 无 jpeg 列', 'jpeg' not in cols, sorted(cols))
    check('camera_frames 有 file_name 列', 'file_name' in cols)
    check('帧行数 > 0', row_count > 0, '{} 行'.format(row_count))

    print('\n[3] 照片文件')
    fdir = media.frames_dir(db_path)
    files = sorted(glob.glob(os.path.join(fdir, '*.jpg')))
    check('frames 目录存在', os.path.isdir(fdir), fdir)
    check('文件数 == 表行数', len(files) == row_count,
          '{} 文件 / {} 行'.format(len(files), row_count))
    missing = [n for n in names if not os.path.exists(os.path.join(fdir, n))]
    check('每个 file_name 都能找到', not missing, '缺失 {}'.format(len(missing)))
    if files:
        head = open(files[0], 'rb').read(2)
        check('文件头是 JPEG (FF D8)', head == b'\xff\xd8', repr(head))
        check('单帧体积合理 (3~200KB)',
              3 * 1024 < os.path.getsize(files[0]) < 200 * 1024,
              '{:.0f} KB'.format(os.path.getsize(files[0]) / 1024))

    print('\n[4] 视频')
    check('camera_videos 有记录', vrow is not None, str(vrow))
    if vrow and vrow[0]:
        vpath = media.video_path(db_path, vrow[0])
        check('视频文件存在', os.path.exists(vpath), vrow[0])
        if os.path.exists(vpath):
            import cv2
            cap = cv2.VideoCapture(vpath)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            cap.release()
            check('视频能打开且帧数 > 0', cap is not None and frames > 0,
                  '{} 帧 @ {:.0f}fps'.format(frames, fps))
            check('视频帧率是 15', abs(fps - 15.0) < 0.6, '{:.1f}'.format(fps))

    print('\n[5] 回放读取')
    refs = camera_frame_refs(db_path)
    check('camera_frame_refs 返回引用', len(refs) == row_count, '{} 条'.format(len(refs)))
    check('引用走文件路径而不是字节', all('jpeg_path' in r for r in refs) if refs else False)
    rows = load_camera_rows(db_path)
    check('load_camera_rows 能读到字节', bool(rows) and all(r[2][:2] == b'\xff\xd8'
                                                          for r in rows if r[2]))

    print('\n[6] 缺帧容错')
    if files:
        os.remove(files[0])
        try:
            pk = load_packets(db_path, include=('camera',))
            cams = [p for p in pk if p[1] == 'camera']
            # 新库的 payload 只有 jpeg_path，要按"文件是否存在"判断，不能只看 jpeg 键
            broken = 0
            for _t, _k, ref in cams:
                if ref.get('jpeg') is not None:
                    continue
                path = ref.get('jpeg_path')
                if not path or not os.path.exists(path):
                    broken += 1
            check('删掉一帧后回放不抛异常', True,
                  '{} 个包，其中 {} 个指向缺失文件（回放给空帧）'.format(len(cams), broken))
            check('缺失帧数正好是删掉的 1 个', broken == 1, str(broken))
        except Exception as exc:                      # noqa: BLE001
            check('删掉一帧后回放不抛异常', False, str(exc))

    print('\n[7] 老库兼容')
    old_db = os.path.join(tmp, 'old.db')
    sample = open(files[-1], 'rb').read() if files else b''
    conn = sqlite3.connect(old_db)
    conn.execute("""CREATE TABLE camera_frames (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL, camera TEXT, frame_index INTEGER NOT NULL,
                    width INTEGER, height INTEGER, jpeg BLOB NOT NULL)""")
    for i in (1, 2):
        conn.execute('INSERT INTO camera_frames (time,camera,frame_index,width,height,jpeg)'
                     ' VALUES (?,?,?,?,?,?)',
                     ('2026-11-14 12:00:00.00000{}'.format(i), 'usb0', i, 640, 480, sample))
    conn.commit()
    conn.close()
    old_rows = load_camera_rows(old_db)
    check('老库(BLOB)仍能读', len(old_rows) == 2 and old_rows[0][2] == sample,
          '{} 行'.format(len(old_rows)))

    failed = [name for name, ok in results if not ok]
    print('\n通过 {}/{}'.format(len(results) - len(failed), len(results)))
    if failed:
        print('失败项：' + '、'.join(failed))

    shutil.rmtree(tmp, ignore_errors=True)
    print('临时目录已清理')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
