# -*- coding: utf-8 -*-
"""人脸检测：YOLOv8n-face。

两个后端，按可用性自动选：

| 后端 | 依赖 | 说明 |
|---|---|---|
| `torch` | ultralytics + torch（可走 CUDA） | 首选，RTX 3060 上单帧 10~30ms |
| `onnx` | onnxruntime + opencv | 降级，纯 CPU 也能到 10~20fps，不需要 2GB 的 torch |

权重文件（`tools/vision/fetch_models.py` 获取）：
  * `models/yolov8n-face.pt`    —— torch 后端
  * `models/yolov8n-face.onnx`  —— onnx 后端

为什么用「face」专版而不是通用 yolov8n：COCO 版的 person 类框住的是整个人，
而张嘴分类器要的是**贴合的人脸 ROI**，通用模型给不了。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models')
TORCH_MODEL = os.path.join(MODELS_DIR, 'yolov8n-face.pt')
ONNX_MODEL = os.path.join(MODELS_DIR, 'yolov8n-face.onnx')


@dataclass
class FaceBox:
    """一张人脸。坐标是原图像素，已裁到图内。"""

    x1: int
    y1: int
    x2: int
    y2: int
    score: float = 0.0

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

    def as_tuple(self):
        return self.x1, self.y1, self.x2, self.y2


class FaceDetector:
    """YOLOv8n-face 的薄封装，只暴露「给一帧，返回人脸框列表」。"""

    def __init__(self, backend: str = 'auto', conf: float = 0.35, imgsz: int = 640,
                 device: Optional[str] = None):
        self.requested_backend = backend
        self.conf = float(conf)
        self.imgsz = int(imgsz)
        self.device = device
        self.backend = ''
        self.last_latency_ms = 0.0
        self._model = None            # torch 后端：ultralytics.YOLO
        self._net = None              # onnx 后端：cv2.dnn.Net

    # ------------------------------------------------------------------
    def load(self) -> 'FaceDetector':
        """按 auto / torch / onnx 的规则加载，返回自身。"""
        if self.backend:
            return self

        want = self.requested_backend.lower()
        errors = []

        if want in ('auto', 'torch'):
            try:
                self._load_torch()
                self.backend = 'torch'
                return self
            except Exception as exc:                      # noqa: BLE001 - 失败就试下一个
                errors.append('torch 后端不可用：{}'.format(exc))

        if want in ('auto', 'onnx'):
            try:
                self._load_onnx()
                self.backend = 'onnx'
                return self
            except Exception as exc:                      # noqa: BLE001
                errors.append('onnx 后端不可用：{}'.format(exc))

        raise RuntimeError('人脸检测模型加载失败：\n  ' + '\n  '.join(errors) +
                           '\n请先运行 python tools/vision/fetch_models.py')

    def _load_torch(self) -> None:
        if not os.path.exists(TORCH_MODEL):
            raise FileNotFoundError('缺少 {}'.format(os.path.basename(TORCH_MODEL)))
        from ultralytics import YOLO                        # 重依赖，延迟导入

        self._model = YOLO(TORCH_MODEL)
        # 有 CUDA 就用，没有就让 ultralytics 自己回退到 CPU
        self._device = self.device
        if self._device is None:
            try:
                import torch
                self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
            except Exception:                              # noqa: BLE001
                self._device = 'cpu'

    def _load_onnx(self) -> None:
        import cv2

        if not os.path.exists(ONNX_MODEL):
            raise FileNotFoundError('缺少 {}'.format(os.path.basename(ONNX_MODEL)))
        self._net = cv2.dnn.readNetFromONNX(ONNX_MODEL)
        self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

    # ------------------------------------------------------------------
    def detect(self, frame_bgr: np.ndarray) -> List[FaceBox]:
        """返回按面积从大到小排序的人脸框（多张脸时调用方取第一个即可）。"""
        if not self.backend:
            self.load()
        if frame_bgr is None or frame_bgr.size == 0:
            return []

        import time
        started = time.perf_counter()
        boxes = self._detect_torch(frame_bgr) if self.backend == 'torch' \
            else self._detect_onnx(frame_bgr)
        self.last_latency_ms = (time.perf_counter() - started) * 1000.0
        boxes.sort(key=lambda box: box.area, reverse=True)
        return boxes

    # -------------------------------------------------- torch 后端
    def _detect_torch(self, frame_bgr: np.ndarray) -> List[FaceBox]:
        height, width = frame_bgr.shape[:2]
        results = self._model.predict(
            source=frame_bgr, conf=self.conf, imgsz=self.imgsz,
            device=self._device, verbose=False)
        boxes: List[FaceBox] = []
        for result in results:
            if result.boxes is None:
                continue
            for row in result.boxes.data.tolist():          # [x1,y1,x2,y2,score,cls]
                # 注意：_clip 的第 5、6 个参数是 width/height，第 7 个才是 score。
                # 写成 self._clip(*row[:5], width, height) 会把 score 当宽度传进去，
                # 于是每个框都被裁成 0 宽、全部丢弃 —— 表现为「一张脸都检不到」。
                box = self._clip(row[0], row[1], row[2], row[3],
                                 width, height, float(row[4]))
                if box is not None:
                    boxes.append(box)
        return boxes

    # -------------------------------------------------- onnx 后端
    def _detect_onnx(self, frame_bgr: np.ndarray) -> List[FaceBox]:
        """YOLOv8 的 ONNX 输出需要自己解码 —— (1, 4+nc, N) 的锚点矩阵。"""
        import cv2

        height, width = frame_bgr.shape[:2]
        blob = cv2.dnn.blobFromImage(frame_bgr, 1 / 255.0, (self.imgsz, self.imgsz),
                                     swapRB=True, crop=False)
        self._net.setInput(blob)
        output = self._net.forward()
        predictions = np.squeeze(output)

        # 单类人脸模型：前 4 行是 cx,cy,w,h，第 5 行起是类别分数
        if predictions.ndim != 2:
            return []
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.transpose()

        scores = predictions[:, 4:].max(axis=1)
        keep = scores >= self.conf
        if not np.any(keep):
            return []
        predictions = predictions[keep]
        scores = scores[keep]

        scale_x, scale_y = width / self.imgsz, height / self.imgsz
        rects, confidences = [], []
        for row, score in zip(predictions, scores):
            cx, cy, w, h = row[:4]
            x1 = (cx - w / 2) * scale_x
            y1 = (cy - h / 2) * scale_y
            rects.append([int(x1), int(y1), int(w * scale_x), int(h * scale_y)])
            confidences.append(float(score))

        indices = cv2.dnn.NMSBoxes(rects, confidences, self.conf, 0.45)
        boxes: List[FaceBox] = []
        for index in np.array(indices).reshape(-1):
            x, y, w, h = rects[int(index)]
            box = self._clip(x, y, x + w, y + h, width, height, confidences[int(index)])
            if box is not None:
                boxes.append(box)
        return boxes

    # ------------------------------------------------------------------
    @staticmethod
    def _clip(x1, y1, x2, y2, width, height, score: float = 0.0) -> Optional[FaceBox]:
        """裁到图内；裁完太小的丢弃（远小于分类器需要的 100×100 就没意义）。"""
        left = max(0, int(round(x1)))
        top = max(0, int(round(y1)))
        right = min(width, int(round(x2)))
        bottom = min(height, int(round(y2)))
        if right - left < 24 or bottom - top < 24:
            return None
        return FaceBox(left, top, right, bottom, float(score))

    @property
    def description(self) -> str:
        if self.backend == 'torch':
            return 'YOLOv8n-face / torch（{}）'.format(getattr(self, '_device', '?'))
        if self.backend == 'onnx':
            return 'YOLOv8n-face / onnxruntime（CPU）'
        return '未加载'
