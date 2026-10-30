#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 摄像头采集：按目标帧率（默认 30fps）截帧 -> JPEG 入库 -> 供界面预览。

设计（和 BLE 那套保持同样的线程边界）：

    采集线程（cv2.VideoCapture.read）        —— 只读帧、编码、塞给写库队列
        ├─ latest_frame（加锁的一个槽）      —— 界面定时取最新帧显示
        └─ CameraWriter.submit(record)      —— 写库线程批量 executemany + 定期 commit

* **30fps**：摄像头本身可能是 60fps，读到的帧如果早于目标间隔就丢弃（计入 dropped），
  保证入库帧率稳定在目标值；同时统计实测帧率供界面显示。
* **入库**：默认每段录像一个独立库 `data/camera_YYYYmmdd_HHMMSS.db`，
  表 `camera_frames(time, camera, frame_index, width, height, jpeg BLOB)`。
  帧以 JPEG 原始字节存 BLOB，分析端取出来 `cv2.imdecode` 就能用。
* 不把大 BLOB 塞进 BLE 的传感器库里，避免采集库体积失控（30fps/640×480/JPEG80 ≈ 1~1.5MB/s）。

单独运行（无界面）：
    python camera_receiver.py --index 0 --seconds 10
"""
from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from core.config import DATA_DIR

TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_FPS = 30.0
DEFAULT_QUALITY = 80

DDL = [
    """CREATE TABLE IF NOT EXISTS camera_frames (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           time TEXT NOT NULL,
           camera TEXT,
           frame_index INTEGER NOT NULL,
           width INTEGER,
           height INTEGER,
           jpeg BLOB NOT NULL
       )""",
    'CREATE INDEX IF NOT EXISTS idx_camera_frames_time ON camera_frames(time)',
    'CREATE INDEX IF NOT EXISTS idx_camera_frames_index ON camera_frames(frame_index)',
    """CREATE TABLE IF NOT EXISTS camera_sessions (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           started_at TEXT,
           ended_at TEXT,
           camera TEXT,
           width INTEGER,
           height INTEGER,
           target_fps REAL,
           frame_count INTEGER DEFAULT 0
       )""",
]


def now_str() -> str:
    return datetime.now().strftime(TIME_FORMAT)


class CameraWriter(threading.Thread):
    """把帧写进 SQLite：批量插入 + 定期提交，避免每帧一次 fsync 拖慢采集。"""

    def __init__(self, db_path: str, camera: str = 'usb0', target_fps: float = DEFAULT_FPS,
                 batch_rows: int = 30, commit_interval: float = 0.5, max_queue: int = 3000):
        super().__init__(name='camera-writer', daemon=True)
        self.db_path = db_path
        self.camera = camera
        self.target_fps = target_fps
        self.batch_rows = batch_rows
        self.commit_interval = commit_interval
        self.queue: 'queue.Queue[Optional[tuple]]' = queue.Queue(maxsize=max_queue)
        self._stop_event = threading.Event()          # 注意别叫 _stop：Thread 自带 _stop() 方法
        self.stats = {'written': 0, 'dropped': 0, 'errors': 0, 'queue_max': 0}
        self.session_id: Optional[int] = None
        self.conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------- 采集线程调用
    def submit(self, record: tuple) -> None:
        """record = (time, camera, frame_index, width, height, jpeg_bytes)。"""
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.stats['dropped'] += 1
            return
        self.stats['queue_max'] = max(self.stats['queue_max'], self.queue.qsize())

    # ------------------------------------------------------------- 线程主体
    def run(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or '.', exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.execute('PRAGMA busy_timeout=5000')   # 与 BLE 传感器库共用同一个文件时避免 locked
        for ddl in DDL:
            self.conn.execute(ddl)
        self.conn.execute(
            'INSERT INTO camera_sessions (started_at, camera, width, height, target_fps)'
            ' VALUES (?,?,?,?,?)',
            (now_str(), self.camera, None, None, self.target_fps))
        self.session_id = self.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.conn.commit()

        pending = []
        last_commit = time.time()
        while True:
            try:
                record = self.queue.get(timeout=0.05)
            except queue.Empty:
                record = None
            if record is not None:
                pending.append(record)
            now = time.time()
            if pending and (len(pending) >= self.batch_rows
                            or now - last_commit >= self.commit_interval
                            or (record is None and self._stop_event.is_set())):
                try:
                    self.conn.executemany(
                        'INSERT INTO camera_frames (time, camera, frame_index, width, height, jpeg)'
                        ' VALUES (?,?,?,?,?,?)', pending)
                    self.conn.commit()
                    self.stats['written'] += len(pending)
                except Exception as exc:                  # noqa: BLE001 - 写库失败不拖垮采集
                    self.stats['errors'] += 1
                    print('[摄像头] 写库失败：{}'.format(exc), flush=True)
                pending.clear()
                last_commit = now
            if record is None and self._stop_event.is_set() and self.queue.empty():
                break
        if pending:
            try:
                self.conn.executemany(
                    'INSERT INTO camera_frames (time, camera, frame_index, width, height, jpeg)'
                    ' VALUES (?,?,?,?,?,?)', pending)
                self.stats['written'] += len(pending)
            except Exception:                             # noqa: BLE001
                self.stats['errors'] += 1
        if self.conn is not None:
            self.conn.execute('UPDATE camera_sessions SET ended_at=?, frame_count=? WHERE id=?',
                              (now_str(), self.stats['written'], self.session_id))
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


class CameraCapture(threading.Thread):
    """按目标帧率读 USB 摄像头：超出的帧丢弃，处理过的帧交给写库线程 + 界面。"""

    def __init__(self, config: Dict[str, Any], writer: CameraWriter,
                 capture_factory: Any = None):
        super().__init__(name='camera-capture', daemon=True)
        self.config = config
        self.writer = writer
        self.index = int(config.get('index', 0))
        self.backend = str(config.get('backend', 'dshow')).lower()
        self.width = int(config.get('width', DEFAULT_WIDTH))
        self.height = int(config.get('height', DEFAULT_HEIGHT))
        self.fps = float(config.get('fps', DEFAULT_FPS))
        self.quality = int(config.get('quality', DEFAULT_QUALITY))
        self.camera = str(config.get('camera', 'usb0'))
        self.warmup = int(config.get('warmup', 3))
        self._capture_factory = capture_factory
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[Tuple[bytes, int, str]] = None
        self.frame_index = 0
        self.dropped = 0
        self.failures = 0
        self.connected = False
        self.resolution = (self.width, self.height)
        self._frame_times = []
        self.actual_fps = 0.0
        self.error = ''

    # ------------------------------------------------------------- 供界面读取
    def latest_frame(self) -> Optional[Tuple[bytes, int, str]]:
        with self._lock:
            return self._latest

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'frames': self.frame_index,
                'dropped': self.dropped,
                'actual_fps': round(self.actual_fps, 1),
                'connected': self.connected,
                'resolution': '{}x{}'.format(*self.resolution),
                'errors': self.failures,
                'error': self.error,
                'written': self.writer.stats['written'],
                'db_dropped': self.writer.stats['dropped'],
            }

    # ------------------------------------------------------------- 生命周期
    def stop(self) -> None:
        self._stop_event.set()

    def _open_capture(self):
        import cv2

        backends = {'dshow': cv2.CAP_DSHOW, 'msmf': cv2.CAP_MSMF, 'any': cv2.CAP_ANY}
        factory = self._capture_factory
        if factory is not None:
            return factory(self.index, backends.get(self.backend, cv2.CAP_ANY))
        cap = cv2.VideoCapture(self.index, backends.get(self.backend, cv2.CAP_ANY))
        if not cap.isOpened() and self.backend != 'any':
            cap.release()
            cap = cv2.VideoCapture(self.index, cv2.CAP_ANY)
        return cap

    def run(self) -> None:
        import cv2

        cap = self._open_capture()
        if cap is None or not cap.isOpened():
            self.error = '打不开摄像头 index={}'.format(self.index)
            print('[摄像头] ' + self.error, flush=True)
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        for _ in range(max(0, self.warmup)):
            cap.read()
        print('[摄像头] 已打开 index={} {}x{} 目标 {}fps，入库 {}'.format(
            self.index, self.width, self.height, self.fps, self.writer.db_path), flush=True)

        interval = 1.0 / self.fps if self.fps > 0 else 0.0
        next_due = time.monotonic()
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.failures += 1
                    if self.failures >= 30:
                        self.error = '连续 30 次读不到帧'
                        break
                    time.sleep(0.05)
                    continue
                self.failures = 0
                captured_at = now_str()           # 采集时刻（编码之前），与 BLE 的到达时刻同一块系统时钟
                # 有些 USB 摄像头/驱动不认 CAP_PROP_FRAME_WIDTH/HEIGHT（实测 1080p 驱动忽略 640×480），
                # 这里按配置尺寸再缩一次，保证入库分辨率与体积可控。
                if (frame.shape[1] != self.width or frame.shape[0] != self.height):
                    frame = cv2.resize(frame, (self.width, self.height),
                                       interpolation=cv2.INTER_AREA)
                now = time.monotonic()
                if now < next_due:                    # 摄像头比目标帧率快 -> 丢帧
                    self.dropped += 1
                    continue
                # 固定节拍：按上次的预定时刻推进，而不是"从现在起再等 33ms"，
                # 否则编码+入队耗时会被算进周期里（实测 60fps 源只能出 20fps）。
                next_due += interval
                if now - next_due > interval:         # 卡顿太久，重新对齐，避免疯狂补帧
                    next_due = now + interval
                ok, buffer = cv2.imencode('.jpg', frame,
                                          [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if not ok:
                    continue
                jpeg = bytes(buffer)
                self.frame_index += 1
                if not self.connected:
                    self.connected = True
                    self.resolution = (int(frame.shape[1]), int(frame.shape[0]))
                stamp = captured_at
                with self._lock:
                    self._latest = (jpeg, self.frame_index, stamp)
                    self._frame_times.append(now)
                    cutoff = now - 2.0
                    while self._frame_times and self._frame_times[0] < cutoff:
                        self._frame_times.pop(0)
                    if len(self._frame_times) > 1:
                        span = self._frame_times[-1] - self._frame_times[0]
                        if span > 0:
                            self.actual_fps = (len(self._frame_times) - 1) / span
                self.writer.submit((stamp, self.camera, self.frame_index,
                                    int(frame.shape[1]), int(frame.shape[0]), jpeg))
        finally:
            try:
                cap.release()
            except Exception:                             # noqa: BLE001
                pass
            print('[摄像头] 采集结束，共 {} 帧（丢 {}）'.format(self.frame_index, self.dropped),
                  flush=True)


class CameraService:
    """采集 + 写库的生命周期封装（界面只跟这个对象打交道）。"""

    def __init__(self, config: Dict[str, Any], db_path: Optional[str] = None,
                 capture_factory: Any = None):
        self.config = dict(config)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.db_path = db_path or os.path.join(DATA_DIR, 'camera_{}.db'.format(stamp))
        self.writer = CameraWriter(self.db_path,
                                   camera=str(self.config.get('camera', 'usb0')),
                                   target_fps=float(self.config.get('fps', DEFAULT_FPS)))
        self.capture = CameraCapture(self.config, self.writer, capture_factory=capture_factory)
        self.started_at = 0.0

    @property
    def running(self) -> bool:
        return self.capture.is_alive()

    def start(self) -> str:
        if self.running:
            return self.db_path
        self.writer.start()
        self.capture.start()
        self.started_at = time.time()
        return self.db_path

    def stop(self) -> None:
        self.capture.stop()
        if self.capture.is_alive():
            self.capture.join(3.0)
        self.writer.stop()

    def latest_frame(self):
        return self.capture.latest_frame()

    def stats(self) -> Dict[str, Any]:
        stats = self.capture.stats()
        stats['db_path'] = self.db_path
        # WAL 模式下数据先落在 -wal 文件里，大小要把 WAL/SHM 一起算上
        total = 0
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            if os.path.exists(path):
                total += os.path.getsize(path)
        stats['db_size'] = total
        stats['elapsed'] = (time.time() - self.started_at) if self.started_at else 0.0
        return stats


def main() -> int:
    parser = argparse.ArgumentParser(description='USB 摄像头 30fps 采集入库')
    parser.add_argument('--index', type=int, default=0)
    parser.add_argument('--width', type=int, default=DEFAULT_WIDTH)
    parser.add_argument('--height', type=int, default=DEFAULT_HEIGHT)
    parser.add_argument('--fps', type=float, default=DEFAULT_FPS)
    parser.add_argument('--quality', type=int, default=DEFAULT_QUALITY)
    parser.add_argument('--seconds', type=float, default=0, help='0 表示一直采到 Ctrl+C')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    service = CameraService({'index': args.index, 'width': args.width, 'height': args.height,
                             'fps': args.fps, 'quality': args.quality}, db_path=args.db)
    print('数据库：' + service.start())
    first_frame_at = None
    try:
        while True:
            time.sleep(1)
            stats = service.stats()
            print('帧 {frames} · 实测 {actual_fps} fps · 丢 {dropped} · 已入库 {written}'
                  .format(**stats), flush=True)
            # 摄像头打开可能要几秒（DSHOW 预热），时长从"第一帧"开始算，别刚开就退出
            if first_frame_at is None and stats['frames'] > 0:
                first_frame_at = time.time()
            if args.seconds and first_frame_at and time.time() - first_frame_at >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        print('结束：' + service.db_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
