# -*- coding: utf-8 -*-
"""打哈欠检测的后台线程与状态快照。

线程模型（和 `core/writer_thread.py`、`core/device_status.py` 同一套思路）：

    摄像头采集线程（30fps）
        └─ CameraService.latest_frame() → (jpeg, index, stamp)
              └─ YawnMonitor 线程（默认 6~7fps）
                    imdecode → 检人脸 → 判张嘴 → 时序状态机
                        └─ YawnState（加锁）←── GUI 每秒读一次

约定：
  * 推理线程里**不出现任何 Qt 对象**（跨线程碰控件会崩，见 ui/buffers.py 的注释）；
  * 推理慢于采集没关系，永远只取"最新一帧"，旧帧自然被丢弃；
  * 推理出错不打断采集，记到 `YawnState.error` 里让界面显示。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from vision.yawn_logic import YawnLogic, YawnStatus

# 重依赖只导入一次（第一次约 1~2 秒）。之后 start() 再调用是空操作。
_HEAVY_IMPORTED = False


def _preload_heavy_modules() -> None:
    """在主线程里把原生依赖预先加载：torch / ultralytics。

    必须由调用线程执行，否则可能段错误（实测退出码 139，Python 层抓不到异常）。
    这类崩溃的典型栈指向 onnxruntime 的 `_pybind_state.py` 里加载 DLL 那一步：
    多个推理框架各带一份 OpenMP / protobuf，后加载时和已加载的那份撞车。

    现在的处理分两层：
      1. 张嘴分类器默认走 cv2.dnn（见 vision/mouth_state.py），**不再加载
         onnxruntime**，把最容易撞的那一份原生库整个绕开；
      2. 剩下 torch 仍在主线程先导入一遍，避免工作线程首次 import 时握手。

    导入失败不抛：真正原因会在工作线程加载模型时以错误信息显示到界面上。
    """
    global _HEAVY_IMPORTED
    if _HEAVY_IMPORTED:
        return
    for module in ('torch', 'ultralytics'):
        try:
            __import__(module)
        except Exception:                                 # noqa: BLE001 - 缺包时降级
            pass
    _HEAVY_IMPORTED = True


@dataclass
class YawnSnapshot:
    """给界面读的一次快照（纯数据，无锁外引用）。"""

    running: bool = False
    loading: bool = False
    error: str = ''
    backend: str = ''
    state: str = 'no_face'
    state_text: str = '未启动'
    fatigue: str = 'normal'
    fatigue_text: str = '正常'
    prob: float = 0.0
    yawn_count: int = 0
    open_duration: float = 0.0
    faces: List[tuple] = field(default_factory=list)   # [(x1,y1,x2,y2,prob)]
    fps: float = 0.0
    latency_ms: float = 0.0
    threshold: float = 0.5


class YawnState:
    """线程安全的状态容器（写：推理线程；读：GUI）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = YawnSnapshot()

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                if hasattr(self._snapshot, key):
                    setattr(self._snapshot, key, value)

    def snapshot(self) -> YawnSnapshot:
        with self._lock:
            # 浅拷贝即可：faces 是每次更新替换的新列表
            return YawnSnapshot(**vars(self._snapshot))

    def reset(self) -> None:
        with self._lock:
            self._snapshot = YawnSnapshot()


class YawnMonitor(threading.Thread):
    """后台推理线程。

    用法：
        monitor = YawnMonitor(camera_service, state, interval=0.15)
        monitor.start()
        ...
        monitor.stop()          # 会 join，最多等 timeout 秒
    """

    def __init__(self, camera_service, state: YawnState,
                 interval: float = 0.15,
                 threshold: float = 0.5,
                 yawn_min_sec: float = 1.5,
                 cooldown_sec: float = 5.0,
                 dip_grace_sec: float = 0.3,
                 face_backend: str = 'auto',
                 face_conf: float = 0.35,
                 logic: Optional[YawnLogic] = None,
                 detector: Any = None,
                 classifier: Any = None,
                 on_event: Any = None,
                 name: str = 'yawn-monitor'):
        super().__init__(name=name, daemon=True)
        self.camera = camera_service
        self.state = state
        self.interval = max(0.05, float(interval))
        self.face_backend = face_backend
        self.face_conf = float(face_conf)
        self.logic = logic or YawnLogic(threshold=threshold, yawn_min_sec=yawn_min_sec,
                                        cooldown_sec=cooldown_sec,
                                        dip_grace_sec=dip_grace_sec)
        # 判定出一次哈欠时回调：fn({'time': epoch, 'prob': 概率, 'duration': 秒})
        self.on_event = on_event
        self._last_count = 0
        self._stop_event = threading.Event()
        # 允许注入：单测/基准脚本可以塞假模型，不必依赖 torch 或 onnxruntime
        self._detector = detector
        self._classifier = classifier
        self._last_index = -1
        self._frame_times: List[float] = []

    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动前先在**调用线程**（GUI 主线程）里把重依赖导入完。

        这不是优化，是必需：`ultralytics` 首次 import 会连带初始化 torch、
        matplotlib 后端和它自己的日志配置，这些在非主线程里做，在 Windows
        + PyQt5 下会直接触发段错误（实测进程退出码 139，无 Python 异常可抓）。
        导入放到主线程后，工作线程只负责推理，就稳定了。
        """
        self.state.update(loading=True, error='', state_text='模型加载中…')
        _preload_heavy_modules()
        super().start()

    def run(self) -> None:
        state = self.state
        state.update(running=True, loading=True, error='',
                     threshold=self.logic.threshold)

        try:
            if self._classifier is None or self._detector is None:
                from vision.face_detector import FaceDetector
                from vision.mouth_state import MouthClassifier

                if self._classifier is None:
                    self._classifier = MouthClassifier(threshold=self.logic.threshold).load()
                if self._detector is None:
                    self._detector = FaceDetector(backend=self.face_backend,
                                                  conf=self.face_conf)
                    self._detector.load()
        except Exception as exc:                          # noqa: BLE001 - 模型问题要显示在界面上
            state.update(running=False, loading=False, error=str(exc))
            return

        state.update(loading=False, backend=getattr(self._detector, 'description', '已注入'))

        # 预热一次：首帧要建 CUDA 上下文、cuDNN 选算法，实测能到 900ms 以上。
        # 不预热的话界面第一秒会显示「1.1fps / 993ms」，看着像模型很慢，实际不是。
        try:
            import numpy as _np
            blank = _np.zeros((480, 640, 3), dtype=_np.uint8)
            self._classifier.predict(blank[100:220, 240:400])   # 黑帧也能预热分类器
            self._detector.detect(blank)
        except Exception:                                 # noqa: BLE001 - 预热失败不影响主流程
            pass

        try:
            while not self._stop_event.is_set():
                loop_start = time.monotonic()
                try:
                    self._tick()
                except Exception as exc:                  # noqa: BLE001 - 单帧失败不拖垮线程
                    state.update(error='推理出错：{}'.format(exc))

                elapsed = time.monotonic() - loop_start
                wait = self.interval - elapsed
                if wait > 0:
                    self._stop_event.wait(wait)
        finally:
            state.update(running=False, loading=False,
                         fps=0.0,
                         state='no_face', state_text='未启动',
                         faces=[])

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        """取最新帧 → 推理 → 更新状态。"""
        import cv2
        import numpy as np

        latest = self.camera.latest_frame() if self.camera is not None else None
        if latest is None:
            # 摄像头刚启动还没出帧：这是正常状态，不该显示成"异常"
            self._publish_empty()
            return
        jpeg, index, _stamp = latest
        if index == self._last_index:
            return                                        # 没有新帧，省一次推理
        self._last_index = index

        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            self._publish_empty('帧解码失败')
            return

        started = time.perf_counter()
        faces = self._detector.detect(frame)

        prob = 0.0
        boxes: List[tuple] = []
        if faces:
            main = faces[0]                               # 取面积最大的一张脸
            roi = frame[main.y1:main.y2, main.x1:main.x2]
            if roi.size:
                prob = self._classifier.predict(roi)
            # 第一张脸带上概率，界面可以把"张嘴"的框标红
            boxes = [(faces[0].x1, faces[0].y1, faces[0].x2, faces[0].y2, prob)]
            boxes += [(box.x1, box.y1, box.x2, box.y2, 0.0) for box in faces[1:]]

        now = time.monotonic()
        status: YawnStatus = self.logic.update(now, prob, has_face=bool(faces))

        # 计数比上一帧多了 = 刚刚判定出一次哈欠，抛给回调落库。
        # 这里用「计数变化」而不是直接读逻辑里的私有事件列表，是为了让
        # 状态机保持纯函数式、可以被单测随意喂，不必知道库的存在。
        if status.yawn_count > self._last_count:
            self._last_count = status.yawn_count
            self._emit_yawn(status, prob, frame.shape if frame is not None else None)

        total_ms = (time.perf_counter() - started) * 1000.0
        self._frame_times.append(now)
        cutoff = now - 2.0
        while self._frame_times and self._frame_times[0] < cutoff:
            self._frame_times.pop(0)
        fps = 0.0
        if len(self._frame_times) > 1:
            span = self._frame_times[-1] - self._frame_times[0]
            if span > 0:
                fps = (len(self._frame_times) - 1) / span

        self.state.update(
            running=True,
            error='',
            state=status.state,
            state_text=status.state_text,
            fatigue=status.fatigue,
            fatigue_text=status.fatigue_text,
            prob=round(prob, 3),
            yawn_count=status.yawn_count,
            open_duration=status.open_duration,
            faces=boxes,
            fps=round(fps, 1),
            latency_ms=round(total_ms, 1),
        )

    def _publish_empty(self, error: str = '') -> None:
        """没有可用帧时的状态：区分"还没出帧"（正常）与"解码失败"（异常）。"""
        self.state.update(state='no_face', state_text='未检测到人脸',
                          faces=[], prob=0.0, fps=0.0, error=error)

    # ------------------------------------------------------------------
    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def reset_counters(self) -> None:
        """清空哈欠统计（换被试、重新开始采集时用）。"""
        self.logic.reset()
        self._last_count = 0
        self.state.update(yawn_count=0, fatigue='normal', fatigue_text='正常')


def describe(snapshot: YawnSnapshot) -> str:
    """一行摘要，便于日志排查。"""
    return ('{} · {}/{:.2f} · 哈欠 {} 次 · {:.1f}fps · {:.1f}ms · {}'.format(
        snapshot.state_text, snapshot.fatigue_text, snapshot.prob,
        snapshot.yawn_count, snapshot.fps, snapshot.latency_ms, snapshot.backend))
