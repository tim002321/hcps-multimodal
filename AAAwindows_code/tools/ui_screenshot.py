#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面自检：用历史数据把三个页面填满，离屏渲染成 PNG（不需要硬件）。

用法：
    python tools/ui_screenshot.py                     # 默认取 data/ 里数据最全的库
    python tools/ui_screenshot.py --db data/xxx.db --seconds 30 --out ui_shots

顺便做几条断言（心电/手环缓冲有没有数据、心率能不能算出来），所以它既是截图工具也是冒烟测试。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np                                                    # noqa: E402

from ui.buffers import (ECG_RATE, LiveBuffers, ecg_signal_quality,     # noqa: E402
                        estimate_heart_rate)
from ui.replay import list_sessions, load_packets                     # noqa: E402
from ui.window import MonitorWindow, create_app                       # noqa: E402


class FakeCamera:
    """假 USB 摄像头：按 60fps 产出带时间戳的合成画面（让 30fps 截帧逻辑有得丢）。"""

    WIDTH, HEIGHT = 640, 480

    def __init__(self, index=0, backend=0):
        import cv2
        import numpy as np

        self._cv2 = cv2
        self._np = np
        self._opened = True
        self._frames = 0
        self._last = 0.0

    def isOpened(self):                               # noqa: N802
        return self._opened

    def set(self, prop, value):                       # noqa: N802
        return True

    def read(self):                                   # noqa: N802
        time.sleep(1 / 60.0)                          # 模拟 60fps 摄像头
        cv2, np = self._cv2, self._np
        frame = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
        frame[:, :, 0] = np.linspace(20, 90, self.WIDTH, dtype=np.uint8)[None, :]
        frame[:, :, 1] = np.linspace(30, 60, self.HEIGHT, dtype=np.uint8)[:, None]
        self._frames += 1
        x = int((self._frames * 6) % (self.WIDTH - 60))
        cv2.rectangle(frame, (x, 180), (x + 60, 300), (0, 220, 255), -1)
        cv2.putText(frame, 'FAKE USB CAMERA', (18, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (230, 230, 230), 2)
        cv2.putText(frame, 'frame {}'.format(self._frames), (18, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (160, 220, 255), 2)
        return True, frame

    def release(self):                                # noqa: N802
        self._opened = False


def pump(app, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def pick_database(explicit: str = '') -> str:
    """优先挑带手环表的库（这样三页都有内容）。"""
    if explicit:
        return explicit
    candidates = [path for path, _mtime, _size in list_sessions(str(ROOT / 'data'))
                  if path.endswith('.db')]
    for path in candidates:
        try:
            import sqlite3
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
        except Exception:                             # noqa: BLE001
            continue
        if 'bracelet_ppg_data' in tables and 'ecg_data' in tables:
            return path
    return candidates[0] if candidates else ''


def main() -> int:
    parser = argparse.ArgumentParser(description='界面离屏截图 + 冒烟检查')
    parser.add_argument('--db', default='', help='要回放的库/JSONL')
    parser.add_argument('--seconds', type=float, default=30.0, help='取前多少秒的数据')
    parser.add_argument('--out', default=str(ROOT / 'ui_shots'))
    parser.add_argument('--width', type=int, default=1520)
    parser.add_argument('--height', type=int, default=940)
    args = parser.parse_args()

    db_path = pick_database(args.db)
    if not db_path or not os.path.exists(db_path):
        print('[失败] 找不到可回放的库，请用 --db 指定')
        return 1
    print(f'回放数据：{db_path}')
    packets = load_packets(db_path, limit_seconds=args.seconds)
    print(f'重建 {len(packets)} 个数据包')

    buffers = LiveBuffers()

    app = create_app(sys.argv)
    # 采集库与回放源分开：假摄像头的帧写进本次自检库，不去污染回放源
    session_db = os.path.join(str(ROOT / 'data'),
                              'uishot_session_{}.db'.format(time.strftime('%H%M%S')))
    window = MonitorWindow(buffers, session_info={'db_path': session_db,
                                                  'source': '回放（自检）'})
    window.resize(args.width, args.height)
    window.show()
    pump(app, 0.4)

    # 设备准备页要显示"已连接"：BLE 状态直接写进登记表，摄像头用假设备真实跑一遍
    window.status.set('ecg', 'connected', 'E1220511000413',
                      'E1220511000413  MTU=247  信号 -52 dBm')
    window.status.set('bracelet', 'connected', 'MPPB20000008',
                      'MPPB20000008  MTU=247')
    window.camera_factory = lambda i, b: FakeCamera(i, b)
    window.start_camera({'index': 0, 'width': 640, 'height': 480, 'fps': 30.0, 'quality': 80})
    # 用真正的回放器驱动界面（状态页能看到进度条 + 回放预览曲线）
    window.start_replay(db_path, speed=3.0)
    pump(app, 4.0)

    snapshot = buffers.snapshot()
    snapshot['rates'] = {'ecg_samples': ECG_RATE, 'ppg_points': 130.0, 'gsr': 5.0}
    for page in window.pages:
        page.on_data(snapshot)
        page.on_tick(snapshot)
    pump(app, 0.5)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    # 页面数量随导航变化（采集 6 项 + 数据查看/状态识别/数据分析），按实际列表遍历
    for index, page in enumerate(window.pages):
        window.switch_page(index)
        pump(app, 0.4)
        path = out_dir / f'{index + 1}_{page.title}.png'
        window.grab().save(str(path))
        shots.append(path)
        print(f'已保存 {path}')

    # ---- 冒烟断言：缓冲里得有东西，心率要能算出来 ----
    snapshot = buffers.snapshot()
    ecg = np.asarray(snapshot['ecg'], dtype=float)
    quality, quality_text = ecg_signal_quality(ecg, ECG_RATE)
    bpm = estimate_heart_rate(ecg, ECG_RATE)
    ppg_points = sum(len(values) for values in snapshot['ppg'].values())
    checks = [
        ('心电缓冲有数据', ecg.size > 0, f'{ecg.size} 点'),
        # 信号过弱/饱和时必须不报心率；信号正常时算法也可能因间期过于不齐而拒报（合理）
        ('信号差时不报心率', quality == 'ok' or bpm is None, f'quality={quality} bpm={bpm}'),
        ('心率在合理范围', bpm is None or 30 <= bpm <= 200, f'bpm={bpm}'),
        ('手环 PPG 有数据', ppg_points > 0, f'{ppg_points} 点'),
        ('GSR 有数据', len(snapshot['gsr']) > 0, f'{len(snapshot["gsr"])} 点'),
    ]
    camera_stats = window.camera_service.stats() if window.camera_service else {}
    camera_rows = 0
    if camera_stats.get('db_path') and os.path.exists(camera_stats['db_path']):
        conn = sqlite3.connect(f'file:{camera_stats["db_path"]}?mode=ro', uri=True)
        try:
            camera_rows = conn.execute('SELECT COUNT(*) FROM camera_frames').fetchone()[0]
            sample = conn.execute('SELECT jpeg FROM camera_frames LIMIT 1').fetchone()
        finally:
            conn.close()
        if sample:
            import cv2
            image = cv2.imdecode(np.frombuffer(sample[0], dtype=np.uint8), cv2.IMREAD_COLOR)
            checks.append(('入库的 JPEG 能解码', image is not None and image.shape[0] > 0,
                           str(None if image is None else image.shape)))
    checks.append(('摄像头 30fps 入库', camera_rows >= 45,
                   f'2.5 秒写入 {camera_rows} 帧'))
    if camera_stats:
        print(f'  摄像头实测：{camera_stats.get("actual_fps")} fps，'
              f'入库 {camera_stats.get("written")}')
    window.stop_camera()
    failures = []
    print()
    for name, ok, detail in checks:
        print(f'  {"[OK]" if ok else "[!!]"} {name}（{detail}）')
        if not ok:
            failures.append(name)
    print(f'  信号质量：{quality_text}')
    if bpm:
        print(f'  心率估计：{bpm:.1f} bpm')
    print('\n[通过] 界面渲染与数据链路正常' if not failures
          else f'\n[失败] {failures}')
    window.close()
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
