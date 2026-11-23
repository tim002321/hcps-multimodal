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

from vision.pose_gate import gate_face
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
    # ---- 姿态门控（新增）----
    mouth_visible: bool = True       # 本帧的嘴有没有拍到
    gate_reason: str = ''            # '' | no_keypoints | low_conf | head_down | too_small
    hidden_ratio: float = 0.0        # 最近这一秒里"嘴不可见"占的比例，界面直接读


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
                 face_detector: str = 'scrfd',
                 center_band: float = 0.25,
                 gate_enabled: bool = True,
                 kp_conf: float = 0.5,
                 mouth_low_ratio: float = 0.95,
                 mouth_crop: bool = False,
                 logic: Optional[YawnLogic] = None,
                 detector: Any = None,
                 classifier: Any = None,
                 on_event: Any = None,
                 on_frame: Any = None,
                 providers: Optional[List[str]] = None,
                 gpu_fallback: bool = True,
                 name: str = 'yawn-monitor'):
        super().__init__(name=name, daemon=True)
        self.camera = camera_service
        self.state = state
        self.interval = max(0.05, float(interval))
        self.face_backend = face_backend
        self.face_conf = float(face_conf)
        # scrfd = 纯 ONNX 的 det_500m.onnx（默认，不依赖 ultralytics）；
        # 其它值退回 YOLOv8n-face
        self.face_detector = (face_detector or 'scrfd').lower()
        # onnxruntime 的 provider 列表：给 CUDA 就走 GPU，给 CPU 就是纯 CPU
        self.providers = list(providers) if providers else None
        self.gpu_fallback = bool(gpu_fallback)
        # 只认画面中间的人：允许脸中心偏离画面中线多少（按画面宽度的比例）。
        # 0.25 = 中间 50% 宽度这个带子里。旁边的人、背景里的人一律不算。
        self.center_band = float(center_band)
        # 姿态门控：看得见嘴才判；看不见就报 mouth_hidden，不硬给概率
        self.gate_enabled = bool(gate_enabled)
        self.kp_conf = float(kp_conf)
        self.mouth_low_ratio = float(mouth_low_ratio)
        # 喂给张嘴模型的图像范围：False=整张脸（默认，模型就是按整脸训的），
        # True=关键点裁出的嘴部小块。门控在两种模式下都生效，只负责"判不判"。
        # 2026-11-18 实测：同一段录像、同一个模型，嘴部小块在这段上几乎恒为 0.00
        # （明显大张嘴也只给 0.00~0.15），换成整脸立刻回到 0.6~1.00。
        self.mouth_crop = bool(mouth_crop)
        self.logic = logic or YawnLogic(threshold=threshold, yawn_min_sec=yawn_min_sec,
                                        cooldown_sec=cooldown_sec,
                                        dip_grace_sec=dip_grace_sec)
        # 判定出一次哈欠时回调：fn({'time': epoch, 'prob': 概率, 'duration': 秒, ...})
        self.on_event = on_event
        # 每秒一条的汇总回调：fn({...})，由界面写进 vision_frames 表
        self.on_frame = on_frame
        self._last_count = 0
        self._stop_event = threading.Event()
        # 允许注入：单测/基准脚本可以塞假模型，不必依赖 torch 或 onnxruntime
        self._detector = detector
        self._classifier = classifier
        self._last_index = -1
        self._frame_times: List[float] = []
        # 每秒汇总的累加器（_flush_second 时清空）
        self._sec: Optional[int] = None
        self._acc = self._new_acc()

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
                from vision.face_detector import build_detector
                from vision.mouth_state import MouthClassifier

                if self._classifier is None:
                    self._classifier = MouthClassifier(
                        threshold=self.logic.threshold,
                        providers=self.providers).load()
                if self._detector is None:
                    self._detector = build_detector(self.face_detector,
                                                    conf=self.face_conf,
                                                    providers=self.providers,
                                                    gpu_fallback=self.gpu_fallback,
                                                    backend=self.face_backend)
        except Exception as exc:                          # noqa: BLE001 - 模型问题要显示在界面上
            state.update(running=False, loading=False, error=str(exc))
            return

        state.update(loading=False, backend=self._backend_name())

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
            try:
                self._flush_second(time.time())           # 收尾时把最后一秒的汇总吐出去
            except Exception:                             # noqa: BLE001
                pass
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
        main = self._pick_main_face(faces, frame.shape)
        mouth_visible = True
        gate_reason = ''
        if main is not None:
            # 先过姿态门控：决定喂嘴部小块还是退回整脸，以及能不能判
            gate = gate_face(main, frame.shape,
                             kp_conf=self.kp_conf,
                             low_ratio=self.mouth_low_ratio,
                             enabled=self.gate_enabled)
            mouth_visible = gate.visible
            gate_reason = gate.reason
            if mouth_visible:
                if self.mouth_crop:
                    x1, y1, x2, y2 = gate.roi
                else:
                    # 默认整脸：模型只在整脸输入上有效，门控只用来否决
                    x1, y1, x2, y2 = main.x1, main.y1, main.x2, main.y2
                roi = frame[max(0, y1):y2, max(0, x1):x2]
                if roi.size:
                    prob = self._classifier.predict(roi)
            else:
                # 判不出来就给哨兵值 -1：界面据此显示"低头"，而不是假的 0.00
                prob = -1.0
            # 只输出这一张脸：画面里其它人（同事经过、背景有人）既不评分也不画框，
            # 否则他们的哈欠会被记到当前被试头上。仍用 5 元组，画框用整脸坐标。
            boxes = [(main.x1, main.y1, main.x2, main.y2, prob)]

        now = time.monotonic()
        status: YawnStatus = self.logic.update(now, max(prob, 0.0),
                                               has_face=main is not None,
                                               mouth_visible=mouth_visible)

        # 计数比上一帧多了 = 刚刚判定出一次哈欠，抛给回调落库。
        # 用 total_count（会话累计、只增不减）而不是 yawn_count（10 分钟滑窗，
        # 事件过期后会变少），否则滑窗一回落就会漏掉后面的哈欠。
        total = int(getattr(status, 'total_count', status.yawn_count) or 0)
        if total > self._last_count:
            self._last_count = total
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
            yawn_count=total,
            open_duration=status.open_duration,
            faces=boxes,
            fps=round(fps, 1),
            latency_ms=round(total_ms, 1),
            backend=self._backend_name(),
            mouth_visible=mouth_visible,
            gate_reason=gate_reason,
            hidden_ratio=self._hidden_ratio(),
        )

        # 每秒一条的汇总（落库用）：把这一秒内的推理结果压成一行
        self._accumulate(now, status, prob, main, total_ms, fps, mouth_visible,
                         gate_reason)

    # -------------------------------------------------- 每秒汇总
    @staticmethod
    def _new_acc() -> dict:
        return {'frames': 0, 'face_frames': 0, 'hidden_frames': 0,
                'probs': [], 'states': {},
                'conf_max': 0.0, 'latency': [], 'box': None, 'fatigue': 'normal',
                'gate_reason': ''}

    def _hidden_ratio(self) -> float:
        """当前这一秒里「嘴不可见」占的比例，给界面显示用。

        在 _accumulate 之前调用，所以是"本秒已完成帧"的比例，
        差一帧，15fps 下可以忽略。
        """
        acc = self._acc
        frames = acc.get('frames', 0)
        if not frames:
            return 0.0
        return round(acc.get('hidden_frames', 0) / float(frames), 3)

    def _accumulate(self, now: float, status, prob: float, main, latency_ms: float,
                    fps: float, mouth_visible: bool = True,
                    gate_reason: str = '') -> None:
        """把一帧的推理结果并进"当前秒"的累加器，跨秒时吐出上一条。"""
        sec = int(now)
        if self._sec is None:
            self._sec = sec
        elif sec != self._sec:
            self._flush_second(time.time())
            self._sec = sec

        acc = self._acc
        acc['frames'] += 1
        if main is not None:
            acc['face_frames'] += 1
            if mouth_visible:
                acc['probs'].append(float(prob))
            else:
                acc['hidden_frames'] += 1
            acc['conf_max'] = max(acc['conf_max'], float(getattr(main, 'score', 0.0) or 0.0))
            acc['box'] = [int(main.x1), int(main.y1), int(main.x2), int(main.y2)]
        acc['states'][status.state] = acc['states'].get(status.state, 0) + 1
        if gate_reason:
            acc['gate_reason'] = gate_reason
        acc['fatigue'] = status.fatigue
        acc['latency'].append(float(latency_ms))
        acc['fps'] = float(fps)

    def _flush_second(self, epoch: float) -> None:
        """把累加器压成一条记录交给 on_frame。没有帧就跳过（不写空行）。"""
        acc, self._acc = self._acc, self._new_acc()
        if not acc['frames']:
            return
        probs = acc['probs']
        state = max(acc['states'], key=acc['states'].get) if acc['states'] else 'no_face'
        record = {
            'time': float(epoch),
            'has_face': bool(acc['face_frames']),
            'face_ratio': round(acc['face_frames'] / float(acc['frames']), 3),
            'face_conf': round(acc['conf_max'], 3),
            'prob_mean': round(float(sum(probs) / len(probs)), 3) if probs else 0.0,
            'prob_max': round(max(probs), 3) if probs else 0.0,
            'mouth_visible': bool(acc['face_frames'] and not acc['hidden_frames']),
            'gate_reason': acc.get('gate_reason', ''),
            'hidden_ratio': round(acc['hidden_frames'] / float(acc['frames']), 3)
            if acc['frames'] else 0.0,
            'state': state,
            'fatigue': acc['fatigue'],
            'yawn_count': int(self._last_count),
            'open_duration': 0.0,
            'box': acc['box'],
            'infer_fps': round(acc.get('fps', 0.0), 1),
            'latency_ms': round(sum(acc['latency']) / len(acc['latency']), 1),
            'backend': self._backend_name(brief=True),
        }
        if self.on_frame is not None:
            try:
                self.on_frame(record)
            except Exception:                             # noqa: BLE001 - 落库失败不影响检测
                pass

    def _backend_name(self, brief: bool = False) -> str:
        """一句话说明当前推理跑在哪，写进界面和数据库。"""
        detector = self._detector
        label = getattr(detector, 'device_label', '') or ''
        desc = getattr(detector, 'description', '') or '已注入'
        engine = getattr(self._classifier, 'engine', '') or ''
        if brief:
            return '{}|{}'.format(desc.split(' / ')[-1], engine) if engine else desc
        return '{} · 张嘴 {}'.format(desc, engine) if engine else desc

    def _emit_yawn(self, status, prob: float, shape) -> None:
        """一次哈欠落库（on_event）。这个函数以前漏掉了定义，
        导致每次判定哈欠都抛 AttributeError —— yawn_events 表因此从来没写进去过。"""
        if self.on_event is None:
            return
        payload = {
            'time': time.time(),
            'prob': round(float(prob), 3),
            'duration': round(float(getattr(status, 'open_duration', 0.0) or 0.0), 2),
            'yawn_count': int(getattr(status, 'total_count', status.yawn_count) or 0),
            'fatigue': status.fatigue,
            'backend': self._backend_name(brief=True),
        }
        try:
            self.on_event(payload)
        except Exception:                                 # noqa: BLE001 - 落库失败不影响检测
            pass

    def _pick_main_face(self, faces: List[Any], shape) -> Any:
        """只挑画面中间那一个人（规则见 vision/face_detector.pick_center_face）。"""
        from vision.face_detector import pick_center_face
        width = shape[1] if len(shape) > 1 else 0
        return pick_center_face(faces, width, self.center_band)

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
