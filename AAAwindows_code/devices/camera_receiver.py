#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 摄像头采集：按目标帧率截帧 -> 图片存文件夹 + 视频存 mp4 -> 库里只留索引。

为什么不再把 JPEG 塞进数据库
    一帧 640×480/JPEG80 约 40~70 KB，15fps 一小时 2.5~3.7 GB。以前这些 BLOB
    全在会话库的 camera_frames 里，导致：一场会话的 .db 动辄几 GB，翻页/查询/
    拷贝都别扭；回放要把整表读进内存（一小时 54k 帧 ≈ 3 GB）。
    现在图片和视频落到会话目录下的文件夹，库里只留 (时间, 帧号, 相对路径)，
    一个会话的索引不到 1 MB。

落盘结构（都在会话库所在目录下）
    camera_<时间戳>/
        frames/000001.jpg …     逐帧图片（帧号命名，排序即时间序）
        video_<时间戳>.mp4      整段录像（合成失败时退成 .avi/MJPG）
    <会话库>.db
        camera_frames(time, camera, frame_index, width, height, jpeg, file)
            jpeg 留空（老版本是 BLOB），file 是相对会话库目录的路径
        camera_sessions(..., dir, video, video_frames)

线程边界（和 BLE 那套一致）：
    采集线程（cv2.VideoCapture.read）—— 只读帧、编码、塞给写盘队列
        ├─ latest_frame（加锁的一个槽）—— 界面/推理线程取最新帧
        └─ CameraWriter.submit(record) —— 写盘线程：存图 + 合成视频 + 写索引

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
DEFAULT_FPS = 15.0     # 与 config.ini 的 [camera] fps 保持一致；哈欠检测只需 6~7Hz
DEFAULT_QUALITY = 80
# 每 1000 帧一个子目录：一场 100 分钟 15fps 有 9 万帧，平铺在一个目录里
# Windows 资源管理器打开要十几秒，分桶后每层最多 1000 个文件。
FRAMES_PER_DIR = 1000


def frame_rel(index: int) -> str:
    """帧文件相对 frames/ 的路径：0001/000123.jpg。"""
    index = int(index)
    return '{:04d}/{:06d}.jpg'.format(index // FRAMES_PER_DIR, index)

DDL = [
    """CREATE TABLE IF NOT EXISTS camera_frames (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           time TEXT NOT NULL,
           camera TEXT,
           frame_index INTEGER NOT NULL,
           width INTEGER,
           height INTEGER,
           jpeg BLOB,
           file TEXT
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
           frame_count INTEGER DEFAULT 0,
           dir TEXT,
           video TEXT,
           video_frames INTEGER DEFAULT 0
       )""",
]

# 老库升级：camera_frames 少了 file 列，camera_sessions 少了三列
MIGRATE_COLUMNS = {
    'camera_frames': [('file', 'TEXT')],
    'camera_sessions': [('dir', 'TEXT'), ('video', 'TEXT'), ('video_frames', 'INTEGER')],
}


def now_str() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def ensure_columns(conn: sqlite3.Connection) -> None:
    """给老版本的库补列（同一个会话里重启摄像头会复用已有表）。"""
    for table, columns in MIGRATE_COLUMNS.items():
        try:
            have = {row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for name, kind in columns:
            if name not in have:
                try:
                    conn.execute('ALTER TABLE "%s" ADD COLUMN %s %s' % (table, name, kind))
                except sqlite3.Error:
                    pass


def open_video(path: str, fps: float, size: Tuple[int, int]):
    """开一个录像文件；mp4v 不可用时退成 avi/MJPG。返回 (writer, 实际路径)。"""
    import cv2

    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    for candidate, fourcc in ((path, 'mp4v'), (os.path.splitext(path)[0] + '.avi', 'MJPG')):
        try:
            writer = cv2.VideoWriter(candidate, cv2.VideoWriter_fourcc(*fourcc),
                                     float(fps), size)
        except Exception:                                 # noqa: BLE001
            continue
        if writer is not None and writer.isOpened():
            return writer, candidate
        try:
            writer.release()
        except Exception:                                 # noqa: BLE001
            pass
    return None, ''


class CameraWriter(threading.Thread):
    """把帧写到文件夹：图片逐张存、视频连续合成、库里只写索引。"""

    def __init__(self, db_path: str, camera: str = 'usb0', target_fps: float = DEFAULT_FPS,
                 frames_dir: Optional[str] = None, video_path: Optional[str] = None,
                 save_frames: bool = True, save_video: bool = True,
                 batch_rows: int = 30, commit_interval: float = 0.5, max_queue: int = 3000):
        super().__init__(name='camera-writer', daemon=True)
        self.db_path = db_path
        self.camera = camera
        self.target_fps = target_fps
        self.batch_rows = batch_rows
        self.commit_interval = commit_interval
        self.save_frames = bool(save_frames)
        self.save_video = bool(save_video)

        base = os.path.dirname(os.path.abspath(db_path))
        self.frames_dir = frames_dir or os.path.join(base, 'camera_frames')
        self.video_path = video_path or ''
        self.data_dir = base                     # file 列存相对这个目录的路径

        self.queue: 'queue.Queue[Optional[tuple]]' = queue.Queue(maxsize=max_queue)
        self._stop_event = threading.Event()          # 注意别叫 _stop：Thread 自带 _stop() 方法
        self.stats = {'written': 0, 'dropped': 0, 'errors': 0, 'queue_max': 0,
                      'files': 0, 'video_frames': 0, 'video': ''}
        self._video_failed = False       # 编码器打不开就只存图片，不再每批重试
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

    # ------------------------------------------------------------- 落盘
    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.data_dir).replace(os.sep, '/')
        except ValueError:                        # 跨盘符时退成绝对路径
            return path

    def _flush(self, pending, conn, video) -> None:
        """一批帧：先落盘，再一次性写索引。返回（可能是新建的）视频对象。"""
        import cv2
        import numpy as np

        rows = []
        for stamp, camera, index, width, height, jpeg in pending:
            rel = ''
            if self.save_frames and jpeg:
                rel_in_frames = frame_rel(index)
                path = os.path.join(self.frames_dir, *rel_in_frames.split('/'))
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'wb') as handle:
                        handle.write(jpeg)
                    rel = self._rel(path)
                    self.stats['files'] += 1
                except OSError as exc:
                    self.stats['errors'] += 1
                    print('[摄像头] 存图失败：{}'.format(exc), flush=True)
            # 视频在拿到第一帧、知道分辨率之后才建（摄像头 DSHOW 预热常常要 1 秒以上，
            # 之前用 queue.get(timeout=1) 等首帧，等不到就整场不录像）
            if video is None and self.save_video and jpeg and not self._video_failed:
                video, real_path = open_video(
                    self.video_path or os.path.join(os.path.dirname(self.frames_dir),
                                                    'video.mp4'),
                    self.target_fps, (int(width), int(height)))
                if video is None:
                    self._video_failed = True
                    print('[摄像头] 视频编码器打不开，本次只存图片', flush=True)
                else:
                    self.stats['video'] = real_path
                    print('[摄像头] 录像写：{}'.format(real_path), flush=True)
            if video is not None and jpeg:
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    if frame.shape[1] != width or frame.shape[0] != height:
                        frame = cv2.resize(frame, (width, height),
                                           interpolation=cv2.INTER_AREA)
                    try:
                        video.write(frame)
                        self.stats['video_frames'] += 1
                    except Exception as exc:          # noqa: BLE001
                        self.stats['errors'] += 1
                        print('[摄像头] 写视频失败：{}'.format(exc), flush=True)
            # jpeg 留空串：老库这一列是 NOT NULL，空 BLOB 能过；新库只有索引
            rows.append((stamp, camera, index, width, height, b'', rel))

        try:
            conn.executemany(
                'INSERT INTO camera_frames (time, camera, frame_index, width, height, jpeg, file)'
                ' VALUES (?,?,?,?,?,?,?)', rows)
            conn.commit()
            self.stats['written'] += len(rows)
        except Exception as exc:                      # noqa: BLE001 - 写库失败不拖垮采集
            self.stats['errors'] += 1
            print('[摄像头] 写索引失败：{}'.format(exc), flush=True)
        return video

    # ------------------------------------------------------------- 线程主体
    def run(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or '.', exist_ok=True)
        if self.save_frames:
            try:
                os.makedirs(self.frames_dir, exist_ok=True)
            except OSError as exc:
                self.save_frames = False
                print('[摄像头] 图片目录建不出来，改为只存视频：{}'.format(exc), flush=True)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.execute('PRAGMA busy_timeout=5000')   # 与 BLE 传感器库共用同一个文件时避免 locked
        for ddl in DDL:
            self.conn.execute(ddl)
        ensure_columns(self.conn)

        video = None                # 懒建：等第一帧到了在 _flush 里按实际分辨率开
        self.conn.execute(
            'INSERT INTO camera_sessions (started_at, camera, width, height, target_fps, dir, video)'
            ' VALUES (?,?,?,?,?,?,?)',
            (now_str(), self.camera, None, None, self.target_fps,
             self._rel(os.path.dirname(self.frames_dir)) if self.save_frames else '',
             ''))
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
                video = self._flush(pending, self.conn, video)
                pending.clear()
                last_commit = now
            if record is None and self._stop_event.is_set() and self.queue.empty():
                break
        if pending:
            video = self._flush(pending, self.conn, video)
        if video is not None:
            try:
                video.release()
            except Exception:                             # noqa: BLE001
                pass
        if self.conn is not None:
            self.conn.execute(
                'UPDATE camera_sessions SET ended_at=?, frame_count=?, video_frames=?, video=?'
                ' WHERE id=?',
                (now_str(), self.stats['written'], self.stats['video_frames'],
                 self._rel(self.stats['video']) if self.stats['video'] else '',
                 self.session_id))
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def stop(self, timeout: float = 8.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


class CameraCapture(threading.Thread):
    """按目标帧率读 USB 摄像头：超出的帧丢弃，处理过的帧交给写盘线程 + 界面。"""

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
                'files': self.writer.stats['files'],
                'video_frames': self.writer.stats['video_frames'],
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
        print('[摄像头] 已打开 index={} {}x{} 目标 {}fps，图片存 {}'.format(
            self.index, self.width, self.height, self.fps, self.writer.frames_dir), flush=True)

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
    """采集 + 落盘的生命周期封装（界面只跟这个对象打交道）。"""

    def __init__(self, config: Dict[str, Any], db_path: Optional[str] = None,
                 capture_factory: Any = None):
        self.config = dict(config)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.db_path = db_path or os.path.join(DATA_DIR, 'camera_{}.db'.format(stamp))
        base = os.path.dirname(os.path.abspath(self.db_path))
        # 同一秒内连开两次也不会撞目录
        cam_dir = os.path.join(base, 'camera_{}'.format(stamp))
        suffix = 1
        while os.path.exists(cam_dir):
            cam_dir = os.path.join(base, 'camera_{}_{:02d}'.format(stamp, suffix))
            suffix += 1
        self.cam_dir = cam_dir
        self.frames_dir = os.path.join(cam_dir, 'frames')
        self.video_path = os.path.join(cam_dir, 'video_{}.mp4'.format(stamp))

        save_frames = bool(self.config.get('save_frames', True))
        save_video = bool(self.config.get('save_video', True))
        self.writer = CameraWriter(self.db_path,
                                   camera=str(self.config.get('camera', 'usb0')),
                                   target_fps=float(self.config.get('fps', DEFAULT_FPS)),
                                   frames_dir=self.frames_dir,
                                   video_path=self.video_path,
                                   save_frames=save_frames,
                                   save_video=save_video)
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
        stats['frames_dir'] = self.frames_dir if self.writer.save_frames else ''
        stats['video_path'] = self.writer.stats.get('video', '')
        # WAL 模式下数据先落在 -wal 文件里，大小要把 WAL/SHM 一起算上
        total = 0
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            if os.path.exists(path):
                total += os.path.getsize(path)
        stats['db_size'] = total
        frames_bytes = _dir_size(self.frames_dir) if self.writer.save_frames else 0
        video_bytes = (os.path.getsize(stats['video_path'])
                       if stats['video_path'] and os.path.exists(stats['video_path']) else 0)
        stats['frames_bytes'] = frames_bytes
        stats['video_bytes'] = video_bytes
        stats['total_bytes'] = total + frames_bytes + video_bytes
        stats['elapsed'] = (time.time() - self.started_at) if self.started_at else 0.0
        return stats


def _dir_size(path: str) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return total
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description='USB 摄像头采集（图片/视频存文件夹）')
    parser.add_argument('--index', type=int, default=0)
    parser.add_argument('--width', type=int, default=DEFAULT_WIDTH)
    parser.add_argument('--height', type=int, default=DEFAULT_HEIGHT)
    parser.add_argument('--fps', type=float, default=DEFAULT_FPS)
    parser.add_argument('--quality', type=int, default=DEFAULT_QUALITY)
    parser.add_argument('--seconds', type=float, default=0, help='0 表示一直采到 Ctrl+C')
    parser.add_argument('--db', default=None)
    parser.add_argument('--no-frames', action='store_true', help='只录像，不存逐帧图片')
    parser.add_argument('--no-video', action='store_true', help='只存图片，不录像')
    args = parser.parse_args()

    service = CameraService({'index': args.index, 'width': args.width, 'height': args.height,
                             'fps': args.fps, 'quality': args.quality,
                             'save_frames': not args.no_frames,
                             'save_video': not args.no_video}, db_path=args.db)
    print('索引库：' + service.start())
    print('图片目录：' + service.frames_dir)
    print('录像文件：' + service.video_path)
    first_frame_at = None
    try:
        while True:
            time.sleep(1)
            stats = service.stats()
            print('帧 {frames} · 实测 {actual_fps} fps · 丢 {dropped} · 存图 {files} 张'
                  ' · 录像 {video_frames} 帧 · 共 {total:.1f} MB'
                  .format(total=stats['total_bytes'] / 1048576, **stats), flush=True)
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
