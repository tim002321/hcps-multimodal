# -*- coding: utf-8 -*-
"""人脸检测：Face 数据结构 + 三级后端（torch / onnx / dlib HOG）。

为什么不用主程序的 FaceDetector：
1. 它只暴露 detect() -> List[FaceBox]，没有关键点出口，取 5 点得摸它的私有 _model；
2. 它依赖 core.config.BUNDLE_ROOT，打包后语义不同；
3. 它没有 dlib HOG 兜底，而本工具的 onnx 权重可能缺失。

但默认值与选脸语义必须与主程序一致：conf=0.35、imgsz=640、中线 ±25% 带内取最近。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Protocol

import cv2
import numpy as np

from .params import RunParams


@dataclass
class Face:
    x1: int
    y1: int
    x2: int
    y2: int
    score: float = 0.0
    kps: Optional[np.ndarray] = None      # (5,2) 双眼/鼻尖/两嘴角；dlib 后端为 None

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center_x(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y1 + self.y2) / 2.0


class Backend(Protocol):
    name: str

    def load(self) -> None: ...

    def detect(self, bgr: np.ndarray) -> List[Face]: ...


def _clip(x1: float, y1: float, x2: float, y2: float, width: int, height: int,
          score: float) -> Optional[Face]:
    x1 = max(0.0, min(float(x1), width - 1.0))
    y1 = max(0.0, min(float(y1), height - 1.0))
    x2 = max(0.0, min(float(x2), width - 1.0))
    y2 = max(0.0, min(float(y2), height - 1.0))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return Face(int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2)), float(score))


# ---------------------------------------------------------------- torch
class TorchBackend:
    name = 'torch'

    def __init__(self, params: RunParams):
        self.params = params
        self.model_path = os.path.join(params.resolved_models_dir(), 'yolov8n-face.pt')
        self.device = 'cpu'
        self._model = None

    def load(self) -> None:
        if not os.path.exists(self.model_path):
            raise FileNotFoundError('缺少 {}'.format(self.model_path))
        from ultralytics import YOLO           # 重依赖，延迟导入
        self._model = YOLO(self.model_path)
        want = self.params.device
        if want:
            self.device = want
        else:
            try:
                import torch
                self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
            except Exception:                  # noqa: BLE001
                self.device = 'cpu'

    def detect(self, bgr: np.ndarray) -> List[Face]:
        result = self._model.predict(source=bgr, conf=self.params.conf,
                                     imgsz=self.params.imgsz, device=self.device,
                                     verbose=False)[0]
        out: List[Face] = []
        if result.boxes is None or len(result.boxes) == 0:
            return out
        boxes = result.boxes.xyxy.cpu().numpy()
        scores = result.boxes.conf.cpu().numpy()
        kps = result.keypoints.xy.cpu().numpy() if result.keypoints is not None else None
        height, width = bgr.shape[:2]
        for i, row in enumerate(boxes):
            pts = None
            if kps is not None and i < len(kps) and len(kps[i]) >= 5:
                pts = np.asarray(kps[i][:5], dtype=np.float32)
            face = _clip(row[0], row[1], row[2], row[3], width, height, float(scores[i]))
            if face is not None:
                face.kps = pts
                out.append(face)
        return out


# ---------------------------------------------------------------- onnx
class OnnxBackend:
    name = 'onnx'

    def __init__(self, params: RunParams):
        self.params = params
        self.model_path = os.path.join(params.resolved_models_dir(), 'yolov8n-face.onnx')
        self._net = None

    def load(self) -> None:
        if not os.path.exists(self.model_path):
            raise FileNotFoundError('缺少 {}（可用 tools/vision/fetch_models.py 生成）'.format(
                self.model_path))
        self._net = cv2.dnn.readNetFromONNX(self.model_path)
        self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

    def detect(self, bgr: np.ndarray) -> List[Face]:
        size = int(self.params.imgsz)
        height, width = bgr.shape[:2]
        scale = min(size / height, size / width)
        nw, nh = int(round(width * scale)), int(round(height * scale))
        resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        dx, dy = (size - nw) // 2, (size - nh) // 2
        canvas[dy:dy + nh, dx:dx + nw] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (size, size),
                                     swapRB=True, crop=False)
        self._net.setInput(blob)
        pred = self._net.forward()
        return _decode_yolo_output(pred, width, height, scale, dx, dy, self.params.conf)


def _decode_yolo_output(pred, width, height, scale, dx, dy, conf) -> List[Face]:
    """YOLOv8 输出 (1,4+nc,8400) 或 (1,8400,4+nc)，只取第一类（人脸）。"""
    arr = np.asarray(pred)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T
    out: List[Face] = []
    for row in arr:
        score = float(row[4])
        if score < conf:
            continue
        cx, cy, w, h = [float(v) for v in row[:4]]
        x1 = (cx - w / 2 - dx) / scale
        y1 = (cy - h / 2 - dy) / scale
        x2 = (cx + w / 2 - dx) / scale
        y2 = (cy + h / 2 - dy) / scale
        face = _clip(x1, y1, x2, y2, width, height, score)
        if face is not None:
            out.append(face)
    return out


# ---------------------------------------------------------------- dlib HOG
class DlibHogBackend:
    name = 'dlib'

    def __init__(self, params: RunParams):
        self.params = params
        self._detector = None

    def load(self) -> None:
        import dlib
        self._detector = dlib.get_frontal_face_detector()

    def detect(self, bgr: np.ndarray) -> List[Face]:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        rects, scores, _ = self._detector.run(gray, 1)
        out: List[Face] = []
        for rect, score in zip(rects, scores):
            face = _clip(rect.left(), rect.top(), rect.right(), rect.bottom(),
                         width, height, float(score))
            if face is not None:
                out.append(face)
        return out


# ---------------------------------------------------------------- 链
class DetectorChain:
    """按 auto 规则逐个试验后端，全失败给出中文错误。"""

    def __init__(self, params: RunParams):
        self.params = params
        self.backend: Optional[Backend] = None
        self.tried: List[str] = []

    def load(self) -> 'DetectorChain':
        want = (self.params.backend or 'auto').lower()
        order = {
            'auto': [TorchBackend, OnnxBackend, DlibHogBackend],
            'torch': [TorchBackend],
            'onnx': [OnnxBackend],
            'dlib': [DlibHogBackend],
        }.get(want, [TorchBackend, OnnxBackend, DlibHogBackend])

        errors = []
        for factory in order:
            backend = factory(self.params)
            try:
                backend.load()
            except Exception as exc:          # noqa: BLE001
                errors.append('{}：{}'.format(backend.name, exc))
                continue
            self.backend = backend
            self.tried.append(backend.name)
            return self
        raise RuntimeError('人脸检测模型全部加载失败：\n  ' + '\n  '.join(errors))

    @property
    def name(self) -> str:
        return self.backend.name if self.backend else '(未加载)'

    @property
    def device(self) -> str:
        return getattr(self.backend, 'device', 'cpu') if self.backend else ''

    def detect(self, bgr: np.ndarray) -> List[Face]:
        if self.backend is None:
            raise RuntimeError('检测器未加载')
        return self.backend.detect(bgr)


def pick_primary(faces: List[Face], width: int, policy: str = 'center',
                 band: float = 0.25) -> Optional[Face]:
    """选被试。center：画面中线 ±band 内取面积最大；band 内为空且只有一张脸则用它。

    与主程序 pick_center_face（vision/face_detector.py:65-91）语义一致。
    """
    if not faces:
        return None
    if policy == 'largest' or len(faces) == 1:
        return max(faces, key=lambda f: f.area)

    center = width / 2.0
    low, high = center * (1 - band), center * (1 + band)
    inside = [f for f in faces if low <= f.center_x <= high]
    if inside:
        return max(inside, key=lambda f: f.area)
    return max(faces, key=lambda f: f.area)
