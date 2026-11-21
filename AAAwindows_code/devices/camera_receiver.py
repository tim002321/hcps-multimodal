#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 摄像头采集：按目标帧率截帧 -> 照片落盘 + 视频录制 -> 元数据入库。

设计（和 BLE 那套保持同样的线程边界）：

    采集线程（cv2.VideoCapture.read）        —— 只读帧、编码、塞给写库队列
        ├─ latest_frame（加锁的一个槽）      —— 界面定时取最新帧显示
        └─ CameraWriter.submit(record)      —— 写库线程：照片落盘 + 批量写元数据

* **按目标帧率节拍丢帧**：摄像头本身可能是 30fps 或更高，读到的帧如果早于
  目标间隔就丢弃（计入 dropped），保证入库帧率稳定在目标值；实测帧率供界面显示。
* **照片不进数据库**：帧编码成 JPEG 后写到会话目录的 `frames/` 下，
  库里 `camera_frames` 只记 (time, camera, frame_index, width, height, file_name)。
  这样库体积只有几十 KB，几万张照片也不会把库撑到 GB 级、也不会拖慢回放读取。
* **视频旁路录制**：同一批帧同时交给 `VideoRecorder` 编成 mp4，放在 `video/`。
  录制失败只影响视频，拍照和写库不受影响。
* 目录规则见 `core/media_paths.py`。
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
from core import media_paths as media
from devices.camera_media import FrameStore, VideoRecorder

TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_FPS = 15.0
DEFAULT_QUALITY = 80

DDL = [
    # 照片本体在 frames/ 里，这里只记元数据 + 文件名。
    # 老库仍是 jpeg BLOB 结构（CREATE TABLE IF NOT EXISTS 不会改已有表），
    # 读侧用 PRAGMA table_info 判断，写侧检测到旧结构会退回 BLOB 写入。
    """CREATE TABLE IF NOT EXISTS camera_frames (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           time TEXT NOT NULL,
           camera TEXT,
           frame_index INTEGER NOT NULL,
           width INTEGER,
           height INTEGER,
           file_name TEXT NOT NULL DEFAULT ''
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
    """CREATE TABLE IF NOT EXISTS camera_videos (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           started_at TEXT,
           ended_at TEXT,
           camera TEXT,
           width INTEGER,
           height INTEGER,
           fps REAL,
           frame_count INTEGER DEFAULT 0,
           duration REAL DEFAULT 0,
           file_name TEXT
       )""",
]


def now_str() -> str:
    return datetime.now().strftime(TIME_FORMAT)


class CameraWriter(threading.Thread):
    """把帧写进 SQLite：批量插入 + 定期提交，避免每帧一次 fsync 拖慢采集。"""

    def __init__(self, db_path: str, camera: str = 'usb0', target_fps: float = DEFAULT_FPS,
                 batch_rows: int = 30, commit_interval: float = 0.5, max_queue: int = 3000,
                 frames_dir: Optional[str] = None, video_dir: Optional[str] = None,
                 video_fps: float = 15.0,
                 frame_size: Tuple[int, int] = (DEFAULT_WIDTH, DEFAULT_HEIGHT)):
        super().__init__(name='camera-writer', daemon=True)
        self.db_path = db_path
        self.camera = camera
        self.target_fps = target_fps
        self.batch_rows = batch_rows
        self.commit_interval = commit_interval
        self.queue: 'queue.Queue[Optional[tuple]]' = queue.Queue(maxsize=max_queue)
        self._stop_event = threading.Event()          # 注意别叫 _stop：Thread 自带 _stop() 方法
        self.stats = {'written': 0, 'dropped': 0, 'errors': 0, 'queue_max': 0,
                      'video_frames': 0, 'video_ok': False, 'video_file': '',
                      'video_error': ''}
        self.session_id: Optional[int] = None
        self.conn: Optional[sqlite3.Connection] = None
        # 摄像头真实输出尺寸，由 CameraCapture 在打开后回填。
        # 和 camera_frames.width/height（编码后的目标尺寸）分开记：
        # 两者不一致就说明 cap.set() 没被驱动接受，画面是放大出来的。
        self.native_resolution: Optional[Tuple[int, int]] = None
        # 照片/视频目录；为空表示不落文件（此时退回旧版 BLOB 写入）
        self.frames_dir = frames_dir
        self.video_dir = video_dir
        self.video_fps = float(video_fps)
        self.frame_size = (int(frame_size[0]), int(frame_size[1]))
        self.frames: Optional[FrameStore] = None
        self.video: Optional[VideoRecorder] = None
        self.legacy_schema = False
        self.video_started_at = ''

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

        # 老库的 camera_frames 是 jpeg BLOB 结构、没有 file_name 列。
        # CREATE TABLE IF NOT EXISTS 不会改已有表，所以这里探测一次并退回旧写入方式；
        # 否则 INSERT 会报 no column named file_name，整场采集一条都写不进去。
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(camera_frames)')}
        self.legacy_schema = ('file_name' not in columns) and ('jpeg' in columns)
        if self.legacy_schema:
            print('[摄像头] 目标库是旧版 camera_frames（jpeg BLOB），本次按旧结构写入、照片不另存文件',
                  flush=True)
        elif self.frames_dir:
            self.frames = FrameStore(self.frames_dir)

        if self.video_dir:
            stem = 'video_{}'.format(datetime.now().strftime('%Y%m%d_%H%M%S'))
            self.video = VideoRecorder(self.video_dir, stem,
                                       size=self.frame_size, fps=self.video_fps)
            self.video.start()
            self.video_started_at = now_str()

        self.conn.execute(
            'INSERT INTO camera_sessions (started_at, camera, width, height, target_fps)'
            ' VALUES (?,?,?,?,?)',
            (now_str(), self.camera, None, None, self.target_fps))
        self.session_id = self.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.conn.commit()

        if self.legacy_schema:
            insert_sql = ('INSERT INTO camera_frames (time, camera, frame_index, width, height, jpeg)'
                          ' VALUES (?,?,?,?,?,?)')
        else:
            insert_sql = ('INSERT INTO camera_frames'
                          ' (time, camera, frame_index, width, height, file_name)'
                          ' VALUES (?,?,?,?,?,?)')

        pending = []
        last_commit = time.time()
        while True:
            try:
                record = self.queue.get(timeout=0.05)
            except queue.Empty:
                record = None
            if record is not None:
                row = self._stage(record)
                if row is not None:
                    pending.append(row)
            now = time.time()
            if pending and (len(pending) >= self.batch_rows
                            or now - last_commit >= self.commit_interval
                            or (record is None and self._stop_event.is_set())):
                self._flush(pending, insert_sql)
                pending = []
                last_commit = now
            if record is None and self._stop_event.is_set() and self.queue.empty():
                break
        if pending:
            self._flush(pending, insert_sql)
        self._finish()

    # ------------------------------------------------------------- 写库线程内部
    def _stage(self, record: tuple) -> Optional[tuple]:
        """处理一帧：照片落盘 + 视频入队，返回要写库的那一行；落盘失败返回 None。"""
        stamp, camera, index, width, height, jpeg = record
        if self.legacy_schema:
            if self.video is not None:
                self.video.submit(jpeg, stamp)
            return record

        name = ''
        if self.frames is not None:
            name = self.frames.save(jpeg) or ''
            if not name:
                self.stats['errors'] += 1
                return None           # 照片没写成，就别留一条指向空文件的记录
        if self.video is not None:
            self.video.submit(jpeg, stamp)
        return (stamp, camera, index, width, height, name)

    def _flush(self, rows: list, sql: str) -> None:
        try:
            self.conn.executemany(sql, rows)
            self.conn.commit()
            self.stats['written'] += len(rows)
        except Exception as exc:                  # noqa: BLE001 - 写库失败不拖垮采集
            self.stats['errors'] += 1
            print('[摄像头] 写库失败：{}'.format(exc), flush=True)

    def _finish(self) -> None:
        """停视频、补 camera_videos、收尾 camera_sessions。"""
        if self.video is not None:
            self.video.close()
            info = self.video.info()
            self.stats['video_frames'] = info['frames']
            self.stats['video_ok'] = bool(info['ok'])
            self.stats['video_file'] = info['file']
            self.stats['video_error'] = info['error']
            if info['file'] and self.conn is not None:
                try:
                    self.conn.execute(
                        'INSERT INTO camera_videos (started_at, ended_at, camera, width, height,'
                        ' fps, frame_count, duration, file_name) VALUES (?,?,?,?,?,?,?,?,?)',
                        (self.video_started_at, now_str(), self.camera,
                         info['width'], info['height'], info['fps'],
                         info['frames'], info['duration'],
                         os.path.basename(info['file'])))
                except Exception as exc:              # noqa: BLE001
                    print('[摄像头] 写 camera_videos 失败：{}'.format(exc), flush=True)
            self.video = None

        if self.conn is not None:
            # width/height 记的是摄像头真实输出，target_fps 记的是配置目标；
            # 想知道「set 有没有生效」，比一下这两组的实际帧尺寸即可。
            native_w, native_h = self.native_resolution or (None, None)
            try:
                self.conn.execute(
                    'UPDATE camera_sessions SET ended_at=?, frame_count=?, width=?, height=?'
                    ' WHERE id=?',
                    (now_str(), self.stats['written'], native_w, native_h, self.session_id))
            except Exception as exc:                  # noqa: BLE001
                print('[摄像头] 更新 camera_sessions 失败：{}'.format(exc), flush=True)
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
        self.resolution = (self.width, self.height)     # 入库（编码）尺寸
        self.native_resolution: Optional[Tuple[int, int]] = None   # 摄像头真实输出尺寸
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
                'native_resolution': ('{}x{}'.format(*self.native_resolution)
                                      if self.native_resolution else ''),
                'errors': self.failures,
                'error': self.error,
                'written': self.writer.stats['written'],
                'db_dropped': self.writer.stats['dropped'],
            }

    # ------------------------------------------------------------- 生命周期
    def stop(self) -> None:
        self._stop_event.set()

    def set_target_fps(self, fps: float) -> None:
        """运行时改目标帧率（界面改完立即生效，不需要重开视频流）。

        只是改一个 float，读写都是原子操作；run() 里每帧重算 interval，
        下一帧起就按新节拍走。改分辨率则必须重开（见 eye 之外的 resize 逻辑）。
        """
        fps = float(fps)
        if fps > 0:
            self.fps = fps

    def set_quality(self, quality: int) -> None:
        """运行时改 JPEG 质量，下一帧编码就生效。"""
        quality = int(quality)
        if 1 <= quality <= 100:
            self.quality = quality

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
        # 只设分辨率，**不设 FPS**。
        # 实测（Windows / DSHOW）每次 cap.set 都会销毁并重建整条视频流：
        #   set(HEIGHT) ≈ 1.5s，set(FPS) ≈ 1.6s，而 FPS 对实际出帧没有任何影响
        #   —— 摄像头原生就是 30fps，入库帧率由下面的 next_due 节拍丢弃控制。
        # 删掉 set(FPS) 后，启动到首帧从 4.6s 降到 3.2s；分辨率必须保留，
        # 否则摄像头按 480p 出流、再被 resize 放大到 720p，画面反而更糊。
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # warmup 是为了让自动曝光/白平衡稳定下来，但它不是必须跑满 3 帧：
        # 摄像头刚打开时头几帧可能各要 0.5 秒，跑满反而把首帧推迟 1.5 秒。
        # 给个 1.2 秒的预算，到点就往下走 —— 画面质量由后续帧自然收敛。
        warmup_deadline = time.monotonic() + 1.2
        for _ in range(max(0, self.warmup)):
            ok, frame = cap.read()
            # 记下摄像头**真实**输出尺寸（resize 之前），和入库的目标尺寸区分开：
            # set 被驱动忽略时，库里写的是目标尺寸，但画面其实是低分辨率放大出来的。
            if ok and frame is not None and self.native_resolution is None:
                self.native_resolution = (int(frame.shape[1]), int(frame.shape[0]))
            if time.monotonic() >= warmup_deadline:
                break
        if self.native_resolution is None:
            self.native_resolution = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                                     int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        if self.writer is not None:
            self.writer.native_resolution = self.native_resolution
        print('[摄像头] 已打开 index={} 目标 {}x{}@{}fps，实际输出 {}x{}，入库 {}'.format(
            self.index, self.width, self.height, self.fps,
            self.native_resolution[0], self.native_resolution[1], self.writer.db_path),
            flush=True)

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
                now = time.monotonic()
                # 先判节拍、再 resize：摄像头比目标帧率快的时候，被丢掉的那一半帧
                # 原来也要算一次 INTER_AREA 缩放，纯属白烧 CPU。
                if now < next_due:
                    self.dropped += 1
                    continue
                # 有些 USB 摄像头/驱动不认 CAP_PROP_FRAME_WIDTH/HEIGHT（实测 1080p 驱动忽略 640×480），
                # 这里按配置尺寸再缩一次，保证入库分辨率与体积可控。
                if (frame.shape[1] != self.width or frame.shape[0] != self.height):
                    frame = cv2.resize(frame, (self.width, self.height),
                                       interpolation=cv2.INTER_AREA)
                # 固定节拍：按上次的预定时刻推进，而不是"从现在起再等 33ms"，
                # 否则编码+入队耗时会被算进周期里（实测 60fps 源只能出 20fps）。
                # interval 每帧重算：目标帧率可以运行时改（界面改完立即生效）。
                interval = 1.0 / self.fps if self.fps > 0 else 0.0
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
        # 照片/视频目录从会话库路径推导（见 core/media_paths.py）。
        # config['video']=False 时只拍照不录像。
        self.frames_dir = media.frames_dir(self.db_path)
        self.video_dir = media.video_dir(self.db_path) if self.config.get('video', True) else ''
        width = int(self.config.get('width', DEFAULT_WIDTH))
        height = int(self.config.get('height', DEFAULT_HEIGHT))
        self.writer = CameraWriter(self.db_path,
                                   camera=str(self.config.get('camera', 'usb0')),
                                   target_fps=float(self.config.get('fps', DEFAULT_FPS)),
                                   frames_dir=self.frames_dir,
                                   video_dir=self.video_dir,
                                   video_fps=float(self.config.get('video_fps', 15.0)),
                                   frame_size=(width, height))
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

        # 照片 + 视频的体积**不能用 os.walk 现算**：界面每 40ms 调一次 stats()，
        # 几万个文件遍历一次要上百毫秒，会把界面拖卡。照片用 FrameStore 的累计值，
        # 视频只有一个文件、直接 getsize。
        writer = self.writer
        media_bytes = 0
        frames = getattr(writer, 'frames', None)
        if frames is not None:
            media_bytes += frames.bytes
        # 录像过程中 writer.stats 里的 video_* 还是空的（那是 close() 时才汇总的），
        # 所以优先读 VideoRecorder 自己的实时统计，停录后才回落到 writer.stats。
        recorder = getattr(writer, 'video', None)
        live = recorder.stats if recorder is not None else None
        if live is not None:
            video_file = live.get('file') or ''
            video_ok = bool(live.get('ok'))
            video_error = live.get('error') or ''
            video_frames = int(live.get('frames') or 0)
        else:
            video_file = writer.stats.get('video_file') or ''
            video_ok = bool(writer.stats.get('video_ok'))
            video_error = writer.stats.get('video_error') or ''
            video_frames = int(writer.stats.get('video_frames') or 0)
        if video_file and os.path.exists(video_file):
            try:
                media_bytes += os.path.getsize(video_file)
            except OSError:
                pass
        stats['media_bytes'] = media_bytes
        stats['total_bytes'] = total + media_bytes
        stats['video_ok'] = video_ok
        stats['video_file'] = video_file
        stats['video_error'] = video_error
        stats['video_frames'] = video_frames
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
