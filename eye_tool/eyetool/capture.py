# -*- coding: utf-8 -*-
"""用 ffmpeg 的 DirectShow 后端采集。

为什么不用 OpenCV：实测这台机器上 cv2.CAP_DSHOW 无法真正启用 MJPG，
读回来的 FOURCC 始终是 YUY2，于是 1080p 只有 5 fps、720p 只有 10 fps。
ffmpeg 强制 -vcodec mjpeg 后 1920x1080@60 稳定输出 59~60 fps。

管道里**直接透传摄像头的 MJPG 码流**（-c:v copy），Python 侧 cv2.imdecode 解成彩色：
1080p60 只要 3.5 MB/s，解码 4.5 ms（单线程可跑 225 fps）。
对比：灰度 rawvideo 102 MB/s、彩色 rawvideo 6 MB/帧只能到 30 fps ——
所以彩色反而比灰度更省带宽，因为压缩在摄像头里做掉了。
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from typing import Optional

import numpy as np


class FfmpegCapture:
    """后台线程持续读帧，主线程永远拿最新一帧（丢旧帧，不排队）。"""

    def __init__(self, params):
        self.p = params
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None
        self._latest_index = -1
        self._latest_time = 0.0
        self.read_index = 0
        self._last_returned = -1
        self.dropped = 0
        self.error = ''
        self.width = params.width
        self.height = params.height
        self.mode = (getattr(params, 'pipe', 'mjpeg') or 'mjpeg').lower()
        self.channels = 1 if self.mode == 'gray' else 3
        self.color = self.mode != 'gray'
        self.device = params.device
        self._resolve_device()

    def _resolve_device(self):
        """配置的设备名不在时，自动换成本机实际存在的摄像头。

        换过摄像头（或换了电脑）之后，config 里的名字会失效，ffmpeg 只会报
        “Could not find video device”，报错信息还不容易看懂。这里提前列一遍设备，
        名字对不上就直接用第一个，并在控制台说清楚换成了哪个。
        """
        if not getattr(self.p, 'auto_device', True):
            return
        from .params import list_devices
        names = list_devices(self.p.ffmpeg)
        if not names or self.device in names:
            return
        print('[眼动] 找不到摄像头「{}」，改用「{}」（可用：{}）'.format(
            self.device, names[0], ' / '.join(names)), flush=True)
        self.device = names[0]

    def _cmd(self):
        head = [
            self.p.ffmpeg, '-hide_banner', '-loglevel', 'error',
            '-f', 'dshow',
            '-vcodec', 'mjpeg',
            '-video_size', '{}x{}'.format(self.p.width, self.p.height),
            '-framerate', str(self.p.fps),
            '-i', 'video={}'.format(self.device),
        ]
        if self.mode == 'mjpeg':
            # 直接把摄像头的 MJPG 码流透传出来（-c:v copy 不重编码）：
            # 1080p60 只有 3.5 MB/s，比灰度 rawvideo 的 102 MB/s 还省，
            # 而且解码后是彩色 —— 带宽反而成了原来的 1/30。
            # 4K30 约 7.7 MB/s、395 KB/帧，解码 23 ms/帧（30fps 的预算是 33 ms）。
            return head + ['-c:v', 'copy', '-f', 'mjpeg', '-']
        pix = 'gray' if self.mode == 'gray' else 'bgr24'
        return head + ['-f', 'rawvideo', '-pix_fmt', pix, '-']

    def start(self):
        # bufsize 必须是 0（无缓冲）：给一个 100 MB 的缓冲会让 read() 反复
        # 在缓冲区和返回值之间拷贝，实测吞吐从 48 fps 掉到 3.9 fps
        # stderr 不能丢：摄像头打不开时 ffmpeg 的原因（设备名不对、被占用、
        # 分辨率不支持）全在 stderr 里，丢掉就只剩一句"打不开摄像头"没法查。
        # 也不要用 PIPE：没人读会写满管道把 ffmpeg 卡住。写到临时文件，失败时读。
        self._errlog = tempfile.NamedTemporaryFile(
            mode='w+b', prefix='eyetool_ffmpeg_', suffix='.log', delete=False)
        self._errlog_path = self._errlog.name
        try:
            self.proc = subprocess.Popen(self._cmd(), stdout=subprocess.PIPE,
                                         stderr=self._errlog, bufsize=0)
        except FileNotFoundError:
            raise RuntimeError(
                '找不到 ffmpeg：{}。装一个 ffmpeg 并把所在目录加进 PATH，'
                '或用环境变量 EYE_FFMPEG 指定完整路径。'.format(self.p.ffmpeg))
        except OSError as exc:
            raise RuntimeError('启动 ffmpeg 失败：{}'.format(exc))
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        # 等第一帧，确认参数被接受（ffmpeg 会把不支持的分辨率报在这里）
        deadline = time.time() + 8.0
        while self._latest is None and time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('ffmpeg 启动失败：{}'.format(self._ffmpeg_error()))
            time.sleep(0.05)
        if self._latest is None:
            raise RuntimeError('8 秒内没有收到帧。\n{}'.format(self._ffmpeg_error()))
        return self

    def _ffmpeg_error(self) -> str:
        """把 ffmpeg 的 stderr 尾巴整理成一句可读的原因。"""
        try:
            self._errlog.flush()
        except Exception:                                  # noqa: BLE001
            pass
        text = ''
        try:
            with open(self._errlog_path, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except Exception:                                  # noqa: BLE001
            pass
        lines = [ln.strip() for ln in text.replace('\r', '\n').split('\n') if ln.strip()]
        tail = ' / '.join(lines[-3:]) if lines else '（ffmpeg 没有输出）'
        try:                                   # 取设备清单失败也绝不能盖掉原始报错
            from .params import list_devices
            avail = ' / '.join(list_devices(self.p.ffmpeg) or []) or '未找到摄像头'
        except Exception:                                  # noqa: BLE001
            avail = '（列举失败）'
        return 'ffmpeg 报：{}；当前设备名「{}」，实际可用：{}'.format(
            tail, self.device, avail)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self.error

    def _publish(self, frame):
        with self._lock:
            self._latest = frame
            self.read_index += 1
            self._latest_index = self.read_index
            self._latest_time = time.perf_counter()

    def _loop(self):
        if self.mode == 'mjpeg':
            return self._loop_mjpeg()
        return self._loop_raw()

    def _loop_mjpeg(self):
        """MJPG 码流 → 按 JPEG 头尾（FFD8…FFD9）切帧 → imdecode 还原彩色。

        一次 read 可能带回好几帧（管道是字节流，和帧没有对齐关系）。
        只解最后一帧：中间帧解出来也是要被丢掉的，白花 4.5 ms/帧。
        """
        import cv2

        stdout = self.proc.stdout
        buf = bytearray()
        while not self._stop.is_set():
            chunk = stdout.read(1 << 16)
            if not chunk:
                self.error = 'ffmpeg 输出中断（MJPG 流结束）'
                break
            buf += chunk

            last = None
            skipped = 0
            while True:
                start = buf.find(b'\xff\xd8')
                if start < 0:
                    buf.clear()                     # 找不到帧头，丢弃无效字节
                    break
                end = buf.find(b'\xff\xd9', start + 2)
                if end < 0:
                    if start:
                        del buf[:start]             # 保留不完整的帧，等下次补齐
                    break
                if last is not None:
                    skipped += 1
                last = bytes(buf[start:end + 2])
                del buf[:end + 2]
            if last is None:
                continue
            frame = cv2.imdecode(np.frombuffer(last, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            self.dropped += skipped
            self._publish(frame)

    def _loop_raw(self):
        frame_bytes = self.width * self.height * self.channels
        stdout = self.proc.stdout
        while not self._stop.is_set():
            # 必须读满一帧：管道可能分多次返回（1080p 灰度一帧 2 MB），
            # 之前 stdout.read 一次拿不全就 break，线程静默死掉，
            # 表现是运行一两秒后再也不更新
            chunks = []
            got = 0
            while got < frame_bytes:
                part = stdout.read(min(frame_bytes - got, 1 << 20))
                if not part:
                    chunks = []
                    break
                chunks.append(part)
                got += len(part)
            if got < frame_bytes:
                self.error = 'ffmpeg 输出中断（收到 {}/{} 字节）'.format(got, frame_bytes)
                break
            buf = b''.join(chunks)
            if self.channels == 1:
                frame = np.frombuffer(buf, np.uint8).reshape(self.height, self.width)
            else:
                frame = np.frombuffer(buf, np.uint8).reshape(
                    self.height, self.width, 3)
            self._publish(frame)

    def read(self, timeout: float = 1.0):
        """返回 (frame, index, device_time)：**只返回没给过的新帧**。

        之前用局部变量 last 判断，第一次调用就立刻返回了当前帧，
        然后主循环拿着同一帧空转（实测 361 次迭代只覆盖 2 秒设备时间），
        瞳孔值在重复帧上抖动，眨眼判定全是误报。
        """
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if (self._latest is not None
                        and self._latest_index != self._last_returned):
                    self._last_returned = self._latest_index
                    return self._latest, self._latest_index, self._latest_time
            time.sleep(0.003)
        with self._lock:
            if self._latest is None:
                return None, -1, 0.0
            return None, self._latest_index, self._latest_time

    def stop(self):
        self._stop.set()
        try:
            if getattr(self, '_errlog', None) is not None:
                self._errlog.close()
        except Exception:                                  # noqa: BLE001
            pass
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:                              # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:                          # noqa: BLE001
                    pass
        if self.thread is not None:
            self.thread.join(timeout=2)


class VideoFileCapture:
    """离线视频：接口和 FfmpegCapture 一致，方便同一套流程跑文件。"""

    def __init__(self, path, gray=True):
        import cv2
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError('打不开视频：{}'.format(path))
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.gray = gray
        self.index = 0
        self.dropped = 0

    def start(self):
        return self

    def read(self, timeout: float = 1.0):
        import cv2
        ok, frame = self.cap.read()
        if not ok:
            return None, -1, 0.0
        if self.gray:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.index += 1
        return frame, self.index, self.index / float(self.fps)

    def stop(self):
        self.cap.release()


class ImageFolderCapture:
    """图片序列输入：一个目录（可递归）或单个图片文件。

    和会话帧目录结构兼容：data/<组>/<会话>/camera_<戳>/frames/0000/000001.jpg
    按"目录再文件名"自然排序，保证时序不乱。支持中文路径（cv2.imdecode）。
    """

    EXTS = ('.jpg', '.jpeg', '.png', '.bmp')

    def __init__(self, path, gray=True, recursive=True):
        import glob
        import os
        if os.path.isdir(path):
            pat = os.path.join(path, '**', '*') if recursive else os.path.join(path, '*')
            files = [f for f in glob.glob(pat, recursive=recursive)
                     if os.path.splitext(f)[1].lower() in self.EXTS]
        else:
            files = [path]
        if not files:
            raise RuntimeError('没有找到图片：{}'.format(path))

        def key(p):
            digits = ''.join(ch for ch in os.path.splitext(os.path.basename(p))[0] if ch.isdigit())
            return (os.path.dirname(p), int(digits) if digits else 0, p)

        self.files = sorted(files, key=key)
        self.gray = gray
        self.index = 0
        self.dropped = 0
        first = self._load(self.files[0])
        self.height, self.width = first.shape[:2]
        self.fps = 30.0

    @staticmethod
    def _load(path):
        import cv2
        import numpy as np
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError('解不开图片：{}'.format(path))
        return img

    def start(self):
        return self

    def read(self, timeout: float = 1.0):
        import cv2
        if self.index >= len(self.files):
            return None, -1, 0.0
        img = self._load(self.files[self.index])
        self.index += 1
        if self.gray:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img, self.index, self.index / float(self.fps)

    def stop(self):
        pass
