# -*- coding: utf-8 -*-
"""摄像头采集（A0）：本机 USB/内置摄像头逐帧读，落盘 + emit(video_frame)。

和 A0 里已有的 `video.py` 的区别：

* `video.py` 是**录像**用的（gstreamer 起一条管线，一边写 avi 一边抽 jpg），延迟大，
  而且依赖本机的 gst-launch。
* `camera.py` 是**实时监测**用的：cv2.VideoCapture 直接读摄像头，拿到帧立刻 emit，
  帧的 JPEG 字节放在 `packet.raw` 里，上层（A2 表情识别 / 界面预览）不用再回头读磁盘。

落盘规则与会话一致：`<会话结果目录>/<out_dir>/<frame_pattern>`，并落 data_video_frame 表
（camera / frame_index / path / created_date），所以「实时监测」和「采集存档」是同一路数据。

画面全黑这类情况（隐私挡板、被别的程序占用、远程会话里没有摄像头）本身不是错误——
cv2 照样返回全黑帧。这里把每帧的亮度统计放进 packet.meta（mean/std），
界面据此提示"画面全黑"，比让人对着黑屏猜要强。
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time
from typing import Any, Dict, Optional

import numpy as np

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)

BACKENDS = {'dshow': 700, 'msmf': 1400, 'any': 0}      # cv2.CAP_DSHOW / CAP_MSMF / CAP_ANY

DEFAULT_CONFIG: Dict[str, Any] = {
    'index': 0,
    'backend': 'any',          # dshow / msmf / any（Windows 上 dshow 最稳）
    'width': 640,
    'height': 480,
    'fps': 8.0,                # 每秒取几帧（监测够用，别把 CPU 和表情识别压满）
    'warmup': 5,               # 丢掉前几帧（有些驱动前几帧是全黑）
    'out_dir': 'camera1',
    'frame_pattern': 'cam1_%06d.jpg',
    'jpeg_quality': 80,
    'save_frames': True,       # 是否把帧写成 jpg（关掉就是纯监测）
    'save_every': 1,           # 每 N 帧落一张
    'dark_threshold': 3.0,     # 画面均值低于这个数就当"全黑"，只在 meta 里标记
    'max_read_failures': 30,   # 连续读失败这么多次就当设备掉了，交给基类重连
}


class CameraCollector(BaseCollector):
    """本机摄像头采集器（实时监测 + 可选落盘）。"""

    name = 'camera'
    device = 'video_frame'
    auto_reconnect = True

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None,
                 capture_factory: Any = None):
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(cfg, sink)
        self.index = int(cfg['index'])
        self.backend_name = str(cfg.get('backend', 'any')).lower()
        self.width = int(cfg['width'])
        self.height = int(cfg['height'])
        self.fps = float(cfg['fps'])
        self.warmup = int(cfg['warmup'])
        self.out_dir = str(cfg['out_dir'])
        self.frame_pattern = str(cfg['frame_pattern'])
        self.jpeg_quality = int(cfg['jpeg_quality'])
        self.save_frames = bool(cfg['save_frames'])
        self.save_every = max(1, int(cfg['save_every']))
        self.dark_threshold = float(cfg['dark_threshold'])
        self.max_read_failures = int(cfg['max_read_failures'])
        self.session_dir = cfg.get('session_dir') or ''
        self.label = str(cfg.get('label') or cfg.get('camera') or 'camera1')
        # 假摄像头注入（测试/截图自检用）：构造参数优先，其次配置里给的可调用对象
        self._capture_factory = capture_factory or cfg.get('capture_factory')
        self._cap = None
        self._frames = 0
        self._saved = 0
        self._failures = 0
        self.last_brightness = 0.0

    # ------------------------------------------------------------- 路径
    @property
    def frames_path(self) -> str:
        base = os.path.abspath(self.session_dir) if self.session_dir else os.getcwd()
        return os.path.join(base, self.out_dir)

    def _next_index(self) -> int:
        """续号：目录里已有帧就接着往后排，别覆盖（和 video.py 的 dry_run 一个道理）。"""
        pattern = re.sub(r'%\d*d', r'(\\d+)', self.frame_pattern)
        regex = re.compile(f'^{pattern}$')
        top = 0
        for path in glob.glob(os.path.join(self.frames_path, '*.jpg')):
            match = regex.match(os.path.basename(path))
            if match:
                top = max(top, int(match.group(1)))
        return top

    # ------------------------------------------------------------- 生命周期
    def setup(self) -> None:
        import cv2

        backend = BACKENDS.get(self.backend_name, 0)
        factory = self._capture_factory or (lambda index, be: cv2.VideoCapture(index, be))
        cap = factory(self.index, backend)
        if cap is None or not cap.isOpened():
            raise RuntimeError(f'打不开摄像头 index={self.index}（backend={self.backend_name}）')
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.fps > 0:
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        for _ in range(max(0, self.warmup)):
            cap.read()
        self._cap = cap
        self._frames = 0
        self._saved = 0
        self._failures = 0
        if self.save_frames:
            os.makedirs(self.frames_path, exist_ok=True)
            self._frames = self._next_index()
        real_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self.width)
        real_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or self.height)
        log.info('[camera] 已打开摄像头 index=%s（%dx%d，目标 %.1f 帧/秒，落盘=%s）',
                 self.index, real_w, real_h, self.fps, self.save_frames)

    def run_loop(self) -> None:
        import cv2

        cap = self._cap
        if cap is None:
            raise RuntimeError('摄像头未打开')
        interval = 1.0 / self.fps if self.fps > 0 else 0.0
        while not self.should_stop():
            started = time.time()
            ok, frame = cap.read()
            if not ok or frame is None:
                self._failures += 1
                if self._failures >= self.max_read_failures:
                    raise RuntimeError(f'连续 {self._failures} 次读不到帧，摄像头可能被占用')
                self.sleep(0.1)
                continue
            self._failures = 0
            self._frames += 1
            mean = float(np.mean(frame))
            self.last_brightness = mean
            path = ''
            if self.save_frames and self._frames % self.save_every == 0:
                path = self._write_frame(frame)
                if path:
                    self._saved += 1
            encoded, jpeg = cv2.imencode('.jpg', frame,
                                        [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            self.emit({'camera': self.label, 'index': self._frames,
                       'path': path, 'send_time': now_str()},
                      raw=bytes(jpeg) if encoded else None,
                      width=int(frame.shape[1]), height=int(frame.shape[0]),
                      brightness=round(mean, 2),
                      dark=mean < self.dark_threshold)
            elapsed = time.time() - started
            if interval > elapsed and not self.sleep(interval - elapsed):
                break

    def _write_frame(self, frame: Any) -> str:
        import cv2

        name = self.frame_pattern % self._frames
        path = os.path.join(self.frames_path, name)
        try:
            success, buffer = cv2.imencode('.jpg', frame,
                                          [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if not success:
                return ''
            buffer.tofile(path)          # tofile 能绕开中文路径
            return path
        except Exception as exc:                          # noqa: BLE001 - 存不下也要继续监测
            log.warning('[camera] 写帧失败：%s', exc)
            return ''

    def teardown(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:                             # noqa: BLE001
                log.debug('[camera] 释放摄像头出错', exc_info=True)

    def on_stop(self) -> None:
        self.teardown()

    # ------------------------------------------------------------- 状态
    def camera_stats(self) -> Dict[str, Any]:
        """摄像头自己的计数（帧数、落盘数、最近一帧亮度）。"""
        return {'frames': self._frames, 'saved': self._saved,
                'brightness': round(self.last_brightness, 2)}
