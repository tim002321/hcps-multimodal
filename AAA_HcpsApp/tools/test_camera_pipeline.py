# -*- coding: utf-8 -*-
"""摄像头实时监测测试（不需要硬件：用假的 cv2.VideoCapture 喂帧）。

检查三件事：
1. 采集：帧落成 jpg + 进 data_video_frame 表，包里有 JPEG 字节（界面预览用）
2. 纯监测：save_frames=False 时不落盘，但包照样发
3. 表情识别子进程的整帧协议：`{"id": n, "b64": "<JPEG base64>"}` 能换回表情

用法：
    python tools/test_camera_pipeline.py            # 全自动（假摄像头）
    python tools/test_camera_pipeline.py --real     # 用真的 0 号摄像头（需要硬件）
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A0_data_collect.camera import CameraCollector                  # noqa: E402
from core.A1_data_store import SqliteStore, usable_session_dirs          # noqa: E402

FACE_CLI = str(ROOT / 'tools' / 'face_cli.py')


class FakeCapture:
    """假摄像头：read() 返回一张真实人脸图的 640x480 版本（每 7 帧掺一帧全黑）。"""

    WIDTH, HEIGHT = 640, 480

    def __init__(self, index=0, backend=0, frame_path: str = ''):
        import cv2

        self.index = index
        self._opened = True
        self._frames = 0
        image = None
        if frame_path and os.path.exists(frame_path):
            image = cv2.imdecode(np.fromfile(frame_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            gradient = np.linspace(0, 255, self.WIDTH, dtype=np.uint8)
            image = np.repeat(gradient[None, :, None], self.HEIGHT, axis=0)
            image = np.repeat(image, 3, axis=2)
        self._image = cv2.resize(image, (self.WIDTH, self.HEIGHT))

    def isOpened(self):                               # noqa: N802 - 对齐 cv2 接口
        return self._opened

    def set(self, prop, value):                       # noqa: N802
        return True

    def get(self, prop):                              # noqa: N802
        import cv2

        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self.WIDTH
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self.HEIGHT
        return 0

    def read(self):                                   # noqa: N802
        self._frames += 1
        frame = self._image.copy()
        if self._frames % 7 == 0:
            frame[:] = 0                               # 偶尔来一帧全黑，测"画面全黑"提示
        return True, frame

    def release(self):                                # noqa: N802
        self._opened = False


def count_rows(db_path: str) -> int:
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute('SELECT COUNT(*) FROM data_video_frame').fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def face_service_check(payloads):
    """起一个 face_cli --serve 子进程，按顺序发请求、收结果。"""
    process = subprocess.Popen([sys.executable, FACE_CLI, '--serve', '--config',
                                str(ROOT / 'config' / 'default.yaml')],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    results = []
    try:
        # 常驻模式启动后会先吐一行 {"ready": true}（模型加载完），先吃掉它
        ready = process.stdout.readline().decode('utf-8', 'ignore').strip()
        if '"ready"' not in ready:
            return results, f'子进程没报 ready，第一行是：{ready[:80]}'
        for line in payloads:
            process.stdin.write((line + '\n').encode('utf-8'))
            process.stdin.flush()
            out = process.stdout.readline().decode('utf-8', 'ignore').strip()
            if not out:
                return results, f'子进程没有输出（第 {len(results) + 1} 次请求）'
            results.append(json.loads(out))
    finally:
        try:
            process.stdin.write(b'__quit__\n')
            process.stdin.flush()
            process.wait(timeout=5)
        except Exception:                             # noqa: BLE001
            process.kill()
    return results, ''


def main() -> int:
    parser = argparse.ArgumentParser(description='摄像头实时监测测试')
    parser.add_argument('--seconds', type=float, default=1.5)
    parser.add_argument('--fps', type=float, default=20)
    parser.add_argument('--real', action='store_true', help='用真实摄像头（跳过假的）')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    face_image = str(ROOT / 'models' / 'test_photo2.png')
    failures = []

    def check(name: str, ok: bool, detail: str = '') -> None:
        print(f'  {"[OK]" if ok else "[!!]"} {name}{"" if ok else "  " + detail}')
        if not ok:
            failures.append(f'{name} {detail}'.strip())

    print('\n=== 1. 摄像头采集（帧落盘 + 落库）===')
    dirs = usable_session_dirs(user='smoke', task='camtest', root=str(ROOT / '实验结果'), fresh=True)
    store = SqliteStore(dirs['db_path']).start()
    packets = []
    factory = None if args.real else (lambda index, backend: FakeCapture(index, backend, face_image))

    def sink(packet):
        store.submit(packet)
        packets.append(packet)

    collector = CameraCollector({'index': 0, 'fps': args.fps, 'warmup': 0, 'save_frames': True,
                                 'session_dir': dirs['result_dir'], 'out_dir': 'camera1',
                                 'frame_pattern': 'cam1_%06d.jpg'},
                                sink=sink, capture_factory=factory)
    collector.start()
    time.sleep(args.seconds)
    collector.stop()
    time.sleep(0.3)
    store.stop()

    frames_dir = Path(dirs['result_dir']) / 'camera1'
    files = sorted(frames_dir.glob('*.jpg'))
    rows = count_rows(dirs['db_path'])
    raws = [p for p in packets if p.raw]
    print(f'  帧文件 {len(files)} 个，data_video_frame {rows} 行，带 JPEG 字节的包 {len(raws)} 个')
    if packets:
        meta = packets[0].meta
        print(f'  首帧 meta：{meta.get("width")}x{meta.get("height")} 亮度 {meta.get("brightness")} '
              f'全黑={meta.get("dark")}')
    check('收到了帧包', len(packets) > 0, f'{len(packets)}')
    check('帧落盘了', len(files) > 0, f'{len(files)} 个文件')
    check('进了 data_video_frame 表', rows == len(files), f'{rows} vs {len(files)}')
    check('每个包带 JPEG 字节（界面预览要用）', len(raws) == len(packets))
    check('meta 里有亮度统计（全黑诊断）', all('brightness' in p.meta for p in packets))
    if packets:
        dark = [p for p in packets if p.meta.get('dark')]
        check('全黑帧被标记出来', len(dark) > 0, '假摄像头每 7 帧给一帧全黑')

    print('\n=== 2. 只监测不落盘 ===')
    packets2 = []
    dirs2 = usable_session_dirs(user='smoke', task='camtest_monitor', root=str(ROOT / '实验结果'),
                                fresh=True)
    collector2 = CameraCollector({'index': 0, 'fps': args.fps, 'warmup': 0, 'save_frames': False},
                                 sink=packets2.append, capture_factory=factory)
    collector2.start()
    time.sleep(0.8)
    collector2.stop()
    monitor_dir = Path(dirs2['result_dir']) / 'camera1'
    on_disk = list(monitor_dir.glob('*.jpg')) if monitor_dir.exists() else []
    check('不落盘时没有写文件', len(on_disk) == 0, f'{len(on_disk)} 个文件')
    check('不落盘也照样发包', len(packets2) > 0, f'{len(packets2)}')

    print('\n=== 3. 表情识别子进程的整帧协议 ===')
    import cv2

    image = cv2.imdecode(np.fromfile(face_image, dtype=np.uint8), cv2.IMREAD_COLOR)
    encoded, buffer = cv2.imencode('.jpg', image)
    payloads = [face_image, json.dumps({'id': 42, 'b64': base64.b64encode(buffer.tobytes()).decode()})]
    results, error = face_service_check(payloads)
    if error:
        check('子进程整帧识别', False, error)
    else:
        print(f'  路径方式 -> {results[0].get("emotions")}   整帧方式 -> {results[1].get("emotions")}'
              f' (id={results[1].get("id")})')
        check('路径方式返回表情', results[0].get('emotions') == ['happy'],
              f'实际 {results[0].get("emotions")}')
        check('整帧方式返回表情', results[1].get('emotions') == ['happy'],
              f'实际 {results[1].get("emotions")}')
        check('整帧方式回带 id', results[1].get('id') == 42, f'实际 {results[1].get("id")}')
        check('整帧方式给回了人脸框', len(results[1].get('boxes') or []) == 1)

    ok_all = not failures
    print('\n[通过] 摄像头实时监测链路正常' if ok_all
          else f'\n[失败] {len(failures)} 项没过：{failures}')
    return 0 if ok_all else 1


if __name__ == '__main__':
    raise SystemExit(main())
