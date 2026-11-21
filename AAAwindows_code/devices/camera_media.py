#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头的媒体文件写入：照片落盘 + 视频录制。

照片（FrameStore）：
    由写库线程同步调用。单帧写盘约 0.2ms，比开一个线程再排队更划算，
    也避免"文件还没写完、元数据已经入库"的时序问题。

视频（VideoRecorder）：
    独立线程。解码 + 编码约 7ms/帧，不能放在采集线程里，否则会拖低
    实际入库帧率。队列满时**只丢视频帧**，绝不阻塞采集和拍照。

两者都不碰数据库：写了哪个文件，由调用方把文件名写进 camera_frames。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# 与 camera_receiver.TIME_FORMAT 一致；这里单独定义是为了避免两边互相 import
TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'

VIDEO_FPS = 15.0
VIDEO_MAX_GAP = 6                 # 单次最多补 6 帧（0.4 秒），防长卡顿后疯狂补帧
VIDEO_QUEUE_MAX = 45              # 约 3 秒的缓冲

# 只用 OpenCV 内置 ffmpeg 就能编的两种编码。
# 打包产物里没有 openh264-*.dll，所以 **不能用 avc1 / H264**（一定失败）。
VIDEO_CANDIDATES: Tuple[Tuple[str, str], ...] = (('mp4v', '.mp4'), ('MJPG', '.avi'))


def _to_epoch(stamp: str) -> float:
    """把库里的时间字符串转成 epoch 秒；解析失败退到当前时间。"""
    try:
        return datetime.strptime(stamp, TIME_FORMAT).timestamp()
    except (TypeError, ValueError):
        return time.time()


class FrameStore:
    """把一帧 JPEG 落成独立文件，返回文件名（写进数据库用）。"""

    def __init__(self, frames_dir: str):
        self.dir = frames_dir
        # 文件名前缀带微秒：同一会话里改设备号会重开采集（新的 writer + FrameStore），
        # 序号从 1 重新开始，靠前缀区分，不会覆盖上一段的照片。
        self._prefix = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.seq = 0
        self.bytes = 0
        self.stats = {'written': 0, 'errors': 0, 'last_error': ''}

    def save(self, jpeg: bytes) -> Optional[str]:
        """写一张照片；成功返回文件名，失败返回 None（调用方据此跳过这行元数据）。"""
        if not jpeg:
            return None
        if not os.path.isdir(self.dir):
            try:
                os.makedirs(self.dir, exist_ok=True)      # 懒建：没采到帧就不留空目录
            except OSError as exc:
                self.stats['errors'] += 1
                self.stats['last_error'] = str(exc)
                print('[摄像头] 建照片目录失败：{}'.format(exc), flush=True)
                return None
        self.seq += 1
        name = '{}_{:06d}.jpg'.format(self._prefix, self.seq)
        try:
            with open(os.path.join(self.dir, name), 'wb') as fp:
                fp.write(jpeg)
        except OSError as exc:
            self.stats['errors'] += 1
            self.stats['last_error'] = str(exc)
            print('[摄像头] 写帧失败：{}'.format(exc), flush=True)
            return None
        self.bytes += len(jpeg)
        self.stats['written'] += 1
        return name


class VideoRecorder(threading.Thread):
    """旁路录像：把采集到的同一批 JPEG 编成 mp4（失败自动降级到 avi）。

    时长守恒：容器帧率固定，但源帧间隔不均匀（节拍丢帧、USB 卡顿）。
    按真实时间戳算"这一帧应该占几格"，缺的用上一帧补齐，多出来的抽掉。
    """

    def __init__(self, video_dir: str, file_stem: str,
                 size: Tuple[int, int] = (640, 480), fps: float = VIDEO_FPS,
                 max_queue: int = VIDEO_QUEUE_MAX):
        super().__init__(name='camera-video', daemon=True)
        self.dir = video_dir
        self.file_stem = file_stem
        self.size = (int(size[0]), int(size[1]))
        self.fps = float(fps) if fps and fps > 0 else VIDEO_FPS
        self.queue: 'queue.Queue[Optional[tuple]]' = queue.Queue(maxsize=max_queue)
        self._stop_event = threading.Event()
        self.stats: Dict[str, Any] = {
            'frames': 0,          # 实际写进视频的帧数（含补的）
            'duplicated': 0,      # 其中补帧的数量
            'dropped': 0,         # 源比目标帧率快，被抽掉的
            'errors': 0,
            'ok': False,
            'file': '',
            'error': '',
        }
        self._out = None
        self._failed = False
        self._last_epoch: Optional[float] = None
        self._last_frame = None
        self._consecutive_errors = 0

    # ------------------------------------------------------------- 采集/写库线程调用
    def submit(self, jpeg: bytes, stamp: str) -> None:
        """绝不阻塞：队列满就丢这一帧视频（照片和写库不受影响）。"""
        if self._failed or not jpeg:
            return
        try:
            self.queue.put_nowait((jpeg, stamp))
        except queue.Full:
            self.stats['dropped'] += 1

    def close(self, timeout: float = 3.0) -> None:
        """只发停止信号；release() 留给线程自己在 finally 里做。"""
        self._stop_event.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.is_alive():
            self.join(timeout)

    def info(self) -> Dict[str, Any]:
        data = dict(self.stats)
        if data['frames'] and self.fps > 0:
            data['duration'] = round(data['frames'] / self.fps, 3)
        else:
            data['duration'] = 0.0
        data['width'], data['height'] = self.size
        data['fps'] = self.fps
        return data

    # ------------------------------------------------------------- 线程主体
    def run(self) -> None:
        try:
            while True:
                try:
                    item = self.queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    continue
                if item is None:
                    break
                if self._out is None and not self._open():
                    self._failed = True
                    break
                jpeg, stamp = item
                try:
                    self._write(jpeg, _to_epoch(stamp))
                    self._consecutive_errors = 0
                except Exception as exc:                  # noqa: BLE001 - 视频失败不影响采集
                    self.stats['errors'] += 1
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= 3:
                        self.stats['error'] = '连续 {} 帧写入失败：{}'.format(
                            self._consecutive_errors, exc)
                        print('[摄像头] 视频录制中止：{}'.format(self.stats['error']), flush=True)
                        self._failed = True
                        break
        finally:
            if self._out is not None:
                try:
                    self._out.release()
                except Exception:                         # noqa: BLE001
                    pass
                self._out = None
            self.stats['ok'] = bool(self.stats['frames'])

    # ------------------------------------------------------------- 内部
    def _open(self) -> bool:
        """按候选编码器逐个尝试；全失败就放弃视频（拍照照旧）。"""
        import cv2

        if not os.path.isdir(self.dir):
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as exc:
                self.stats['error'] = '建视频目录失败：{}'.format(exc)
                print('[摄像头] ' + self.stats['error'], flush=True)
                return False

        for fourcc_name, ext in VIDEO_CANDIDATES:
            path = os.path.join(self.dir, self.file_stem + ext)
            try:
                fourcc = cv2.VideoWriter_fourcc(*fourcc_name)
            except Exception:                             # noqa: BLE001
                continue
            for api in (cv2.CAP_FFMPEG, 0):
                try:
                    writer = (cv2.VideoWriter(path, api, fourcc, self.fps, self.size)
                              if api else cv2.VideoWriter(path, fourcc, self.fps, self.size))
                except Exception:                         # noqa: BLE001
                    writer = None
                if writer is not None and writer.isOpened():
                    self._out = writer
                    self.stats['file'] = path
                    # 一打开就算"可用"：界面在录像过程中读的是这个值，
                    # 若等到 close() 才置位，整个录制期间都会显示"录像失败"。
                    self.stats['ok'] = True
                    print('[摄像头] 开始录像 {}（{} @ {}fps）'.format(
                        os.path.basename(path), fourcc_name, self.fps), flush=True)
                    return True
                if writer is not None:
                    try:
                        writer.release()
                    except Exception:                     # noqa: BLE001
                        pass
            print('[摄像头] 编码器 {} 不可用，换下一个'.format(fourcc_name), flush=True)

        self.stats['error'] = 'VideoWriter 打不开（mp4v / MJPG 都失败）'
        print('[摄像头] ' + self.stats['error'] + '，本次不录像', flush=True)
        return False

    def _decode(self, jpeg: bytes):
        import cv2
        import numpy as np

        try:
            frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:                                 # noqa: BLE001
            return None
        if frame is None:
            return None
        if (frame.shape[1], frame.shape[0]) != self.size:
            frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        return frame

    def _write(self, jpeg: bytes, epoch: float) -> None:
        if self._out is None:
            return
        if self._last_epoch is None:
            gap = 1
        else:
            # 这一帧距离上一帧该占几格：0 或负数 = 源太快，抽掉
            gap = int(round((epoch - self._last_epoch) * self.fps))
            if gap <= 0:
                self.stats['dropped'] += 1
                return
            gap = min(gap, VIDEO_MAX_GAP)

        frame = self._decode(jpeg)
        if frame is None:
            return

        if self._last_frame is not None and gap > 1:
            for _ in range(gap - 1):                      # 卡顿期间重复上一帧，保证时长不缩水
                self._out.write(self._last_frame)
                self.stats['duplicated'] += 1
                self.stats['frames'] += 1
        self._out.write(frame)
        self.stats['frames'] += 1
        self._last_frame = frame
        self._last_epoch = epoch
