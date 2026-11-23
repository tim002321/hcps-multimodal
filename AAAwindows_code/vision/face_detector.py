# -*- coding: utf-8 -*-
"""人脸检测：YOLOv8n-face。

三个后端，按可用性自动选：

| 后端 | 依赖 | 说明 |
|---|---|---|
| `ort` | onnxruntime（装 -gpu 版即走 CUDA） | 首选，GPU 上单帧 5~15ms，CPU 上也不慢 |
| `torch` | ultralytics + torch（CUDA 版可走 GPU） | 退路，RTX 3060 上单帧 10~30ms，但依赖包 2GB |
| `onnx` | opencv 的 cv2.dnn | 最后兜底，纯 CPU，不需要任何额外推理引擎 |

`ort` 与 `onnx` 用的是同一个 `models/yolov8n-face.onnx`，只是执行引擎不同：
前者可以指定 CUDA/DirectML，后者固定 CPU。两者的输出解码完全一致，
所以解码逻辑抽在 `_decode_yolo()` 里共用。

权重文件（`tools/vision/fetch_models.py` 获取）：
  * `models/yolov8n-face.pt`    —— torch 后端
  * `models/yolov8n-face.onnx`  —— ort / onnx 后端

为什么用「face」专版而不是通用 yolov8n：COCO 版的 person 类框住的是整个人，
而张嘴分类器要的是**贴合的人脸 ROI**，通用模型给不了。
"""
from __future__ import annotations

import os

from core.paths import resolve_resource_dir
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

MODELS_DIR = resolve_resource_dir('models')
TORCH_MODEL = os.path.join(MODELS_DIR, 'yolov8n-face.pt')
ONNX_MODEL = os.path.join(MODELS_DIR, 'yolov8n-face.onnx')


@dataclass
class FaceBox:
    """一张人脸。坐标是原图像素，已裁到图内。

    `keypoints` 是 5 个面部关键点，形状 (5, 3)，每行 `(置信, x, y)`，
    坐标已从 letterbox 画布反变换回原图。顺序为
    `左眼、右眼、鼻尖、左嘴角、右嘴角`。

    注意列顺序是 **(置信, x, y)** 而不是 ultralytics 常见的 (x, y, 置信)——
    实测 `row[5:21]` 的第一个值是 0.0/0.85 这种小量，若当成 x 会全部落到画面左边缘。
    torch 后端不产关键点，为 None；用到的地方必须能处理 None。
    """

    x1: int
    y1: int
    x2: int
    y2: int
    score: float = 0.0
    keypoints: Optional[np.ndarray] = None

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


def pick_center_face(faces: List[FaceBox], width: int,
                     center_band: float = 0.25) -> Optional[FaceBox]:
    """从多张脸里挑出画面中间那一个（被试），其余一律忽略。

    `detect()` 返回的是按面积降序的列表，直接取第一个等于"谁离镜头近就认谁"——
    旁边坐着同事、有人从镜头后走过时，哈欠会记到别人头上。这里改成按位置挑：

    1. 脸中心落在「中线 ± center_band×画面宽」这条带子里的才算候选；
    2. 候选里取离中线最近的那个（被试就坐在正前方）；
    3. 带子里一个都没有时：画面只有一张脸仍然用它（单人场景，人可能坐得偏），
       有多张脸就返回 None —— 宁可这一帧不判定，也不拿旁边的人凑数。
    """
    if not faces:
        return None
    if width <= 0:
        return faces[0]
    middle = width / 2.0
    limit = width * max(0.02, float(center_band))
    inside = []
    for box in faces:
        offset = abs(float(box.center_x) - middle)
        if offset <= limit:
            inside.append((offset, box))
    if inside:
        inside.sort(key=lambda item: item[0])
        return inside[0][1]
    return faces[0] if len(faces) == 1 else None


class FaceDetector:
    """YOLOv8n-face 的薄封装，只暴露「给一帧，返回人脸框列表」。"""

    def __init__(self, backend: str = 'auto', conf: float = 0.35, imgsz: int = 640,
                 device: Optional[str] = None,
                 providers: Optional[List[str]] = None,
                 gpu_fallback: bool = True):
        self.requested_backend = backend
        self.conf = float(conf)
        self.imgsz = int(imgsz)
        self.device = device
        self.providers = list(providers) if providers else None
        self.gpu_fallback = bool(gpu_fallback)
        self.backend = ''
        self.active_provider = ''     # ort 后端实际生效的 provider（CUDAExecutionProvider / CPUExecutionProvider）
        self.load_notes: List[str] = []   # 加载过程中值得显示给用户的提示
        self.last_latency_ms = 0.0
        self._model = None            # torch 后端：ultralytics.YOLO
        self._net = None              # onnx 后端：cv2.dnn.Net
        self._session = None          # ort 后端：onnxruntime.InferenceSession
        self._input_name = ''
        self._output_name = ''

    # ------------------------------------------------------------------
    def load(self) -> 'FaceDetector':
        """按 ort → torch → onnx 的顺序尝试，返回自身。"""
        if self.backend:
            return self

        want = self.requested_backend.lower()
        errors = []

        # ort 排第一：同一个 ONNX 模型，能指定 GPU，依赖也比 torch 小得多
        if want in ('auto', 'ort'):
            try:
                self._load_ort()
                self.backend = 'ort'
                return self
            except Exception as exc:                      # noqa: BLE001 - 失败就试下一个
                errors.append('onnxruntime 后端不可用：{}'.format(exc))
                self.load_notes.append('GPU 不可用：{}'.format(exc))

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
                           '\n请先运行 python tools/vision/fetch_models.py --with-onnx')

    def _load_ort(self) -> None:
        """onnxruntime 后端。给了 providers 就按它来，拿不到 GPU 再按配置回退。"""
        import onnxruntime as ort

        if not os.path.exists(ONNX_MODEL):
            raise FileNotFoundError('缺少 {}（运行 fetch_models.py --with-onnx）'
                                    .format(os.path.basename(ONNX_MODEL)))

        available = ort.get_available_providers()
        wanted = list(self.providers or ['CUDAExecutionProvider', 'CPUExecutionProvider'])

        # providers 允许两种写法：纯字符串 'CUDAExecutionProvider'，
        # 或 (名称, 选项字典) 元组。比对可用列表时只取名称 ——
        # 直接拿元组去 in 一个字符串列表永远为 False，会让 CUDA 被静默丢掉。
        def _prov_name(entry):
            return entry[0] if isinstance(entry, (tuple, list)) else entry

        # 只保留本机真装了的 provider，否则 sess_options 会直接报错
        usable = [p for p in wanted if _prov_name(p) in available]
        if not usable:
            raise RuntimeError('onnxruntime 没有可用的 provider（需要 {}，实际 {}）'
                               .format('/'.join(wanted), available))

        want_gpu = any(_prov_name(p) != 'CPUExecutionProvider' for p in usable)
        if want_gpu and not self.gpu_fallback:
            usable = [p for p in usable if _prov_name(p) != 'CPUExecutionProvider'] or usable

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(ONNX_MODEL, sess_options=options,
                                       providers=usable)
        active = session.get_providers()
        self._session = session
        self._input_name = session.get_inputs()[0].name
        self._output_name = session.get_outputs()[0].name
        self.active_provider = active[0] if active else 'CPUExecutionProvider'

        if (self.device or '').lower() == 'cuda' and self.active_provider == 'CPUExecutionProvider':
            message = 'CUDA 不可用，已回退 CPU（onnxruntime 提供 {})'.format(available)
            self.load_notes.append(message)
            if not self.gpu_fallback:
                raise RuntimeError(message)

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
        """返回按面积从大到小排序的人脸框。

        注意别直接取 [0] —— 那等于"认离镜头最近的人"，现场有旁人时会认错。
        要选被试请用 `pick_center_face()`，它按位置挑画面中间那个。
        """
        if not self.backend:
            self.load()
        if frame_bgr is None or frame_bgr.size == 0:
            return []

        import time
        started = time.perf_counter()
        if self.backend == 'torch':
            boxes = self._detect_torch(frame_bgr)
        elif self.backend == 'ort':
            boxes = self._detect_ort(frame_bgr)
        else:
            boxes = self._detect_onnx(frame_bgr)
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

    # -------------------------------------------------- onnxruntime 后端
    def _detect_ort(self, frame_bgr: np.ndarray) -> List[FaceBox]:
        """走 onnxruntime（可 GPU）。预处理与 cv2.dnn 共用一套 letterbox。"""
        blob, box = self._letterbox(frame_bgr)
        output = self._session.run([self._output_name], {self._input_name: blob})[0]
        return self._decode_yolo(np.asarray(output), frame_bgr.shape[1],
                                 frame_bgr.shape[0], box)

    # -------------------------------------------------- onnx 后端（cv2.dnn）
    def _detect_onnx(self, frame_bgr: np.ndarray) -> List[FaceBox]:
        """cv2.dnn 执行同一个模型；预处理与解码都与 ort 后端共用。"""
        blob, box = self._letterbox(frame_bgr)
        self._net.setInput(blob)
        output = self._net.forward()
        return self._decode_yolo(np.asarray(output), frame_bgr.shape[1],
                                 frame_bgr.shape[0], box)

    def _letterbox(self, frame_bgr: np.ndarray):
        """等比缩放到 imgsz×imgsz 并灰边填充，返回 (blob, 反变换参数)。

        必须 letterbox 不能用拉伸：模型是按 640×640 含灰边训练的，
        把 640×480 直接拉成 640×640 会让检测框整体偏移约 100px（实测）。
        """
        import cv2

        height, width = frame_bgr.shape[:2]
        scale = min(self.imgsz / float(width), self.imgsz / float(height))
        new_w, new_h = int(round(width * scale)), int(round(height * scale))
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, np.uint8)
        pad_x, pad_y = (self.imgsz - new_w) // 2, (self.imgsz - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = cv2.resize(
            frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (self.imgsz, self.imgsz),
                                     swapRB=True, crop=False)
        return blob, (scale, pad_x, pad_y)

    def _decode_yolo(self, output: np.ndarray, width: int, height: int,
                     box_transform=None) -> List[FaceBox]:
        """解析人脸模型输出，并把坐标从 letterbox 画布映射回原图。

        两种布局都要支持，按形状自动判断：

        * **锚点格式** `(1, 4+nc, N)`（标准 ultralytics 导出）：每列一个候选，
          需要自己按阈值筛 + NMS。这里 N≈8400。
        * **已做 NMS 的行格式** `(1, M, 4+1+K)`（akanametov 的 yolov8n-face 发布版）：
          每行 `x1,y1,x2,y2,conf, keypoints...`，M=300，K=16（5 个关键点）。
        """
        import cv2

        a = np.squeeze(output)
        if a.ndim != 2 or a.size == 0:
            return []

        scale, pad_x, pad_y = box_transform or (1.0, 0, 0)

        def to_original(x1, y1, x2, y2):
            return ((x1 - pad_x) / scale, (y1 - pad_y) / scale,
                    (x2 - pad_x) / scale, (y2 - pad_y) / scale)

        # (M, 4+1+K)：列数在 6~64 之间且行数明显多 —— 判为行格式（已做过 NMS）
        rows_format = (5 < a.shape[1] <= 64 and a.shape[0] > a.shape[1])

        rects, confidences, keypoints = [], [], []
        if rows_format:
            scores = a[:, 4].astype(float)
            keep = scores >= self.conf
            if not np.any(keep):
                return []
            for row, score in zip(a[keep], scores[keep]):
                x1, y1, x2, y2 = to_original(float(row[0]), float(row[1]),
                                             float(row[2]), float(row[3]))
                if x2 <= x1 or y2 <= y1:
                    continue
                rects.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])
                confidences.append(float(score))
                keypoints.append(self._parse_keypoints(row, scale, pad_x, pad_y))
        else:
            predictions = a
            if predictions.shape[0] < predictions.shape[1]:
                predictions = predictions.transpose()          # → (N, 4+nc)
            if predictions.shape[1] < 5:
                return []
            scores = predictions[:, 4:].max(axis=1)
            keep = scores >= self.conf
            if not np.any(keep):
                return []
            predictions, scores = predictions[keep], scores[keep]
            for row, score in zip(predictions, scores):
                cx, cy, w, h = row[:4]
                x1, y1, x2, y2 = to_original(cx - w / 2, cy - h / 2,
                                             cx + w / 2, cy + h / 2)
                rects.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])
                confidences.append(float(score))
                keypoints.append(None)                         # 锚点格式没有关键点

        if not rects:
            return []
        indices = cv2.dnn.NMSBoxes(rects, confidences, self.conf, 0.45)
        boxes: List[FaceBox] = []
        for index in np.array(indices).reshape(-1):
            index = int(index)
            x, y, w, h = rects[index]
            box = self._clip(x, y, x + w, y + h, width, height, confidences[index])
            if box is not None:
                box.keypoints = keypoints[index]
                boxes.append(box)
        return boxes

    @staticmethod
    def _parse_keypoints(row, scale: float, pad_x: int, pad_y: int):
        """从一行输出里取出 5 个关键点，反变换回原图坐标。

        行格式：`[x1,y1,x2,y2,conf, k1v,k1x,k1y, k2v,k2x,k2y, ...]`
        即 `row[5:20]` 是 5 组 **(置信, x, y)**，`row[20]` 是类别分数（不用）。

        列顺序必须按 (置信, x, y) 解：实测 `row[5]` 是 0.0/0.85 这种小量，
        当成 x 会让所有点落到画面左边缘（已验证）。
        """
        if row.size < 20:
            return None
        raw = np.asarray(row[5:20], dtype=float).reshape(5, 3)
        pts = raw.copy()
        pts[:, 1] = (raw[:, 1] - pad_x) / scale
        pts[:, 2] = (raw[:, 2] - pad_y) / scale
        return pts

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
        if self.backend == 'ort':
            tag = 'CUDA' if 'CUDA' in self.active_provider else \
                ('DirectML' if 'Dml' in self.active_provider else 'CPU')
            if tag == 'CPU' and (self.device or '').lower() == 'cuda':
                return 'YOLOv8n-face / onnxruntime（CPU，GPU 不可用）'
            return 'YOLOv8n-face / onnxruntime（{}）'.format(tag)
        if self.backend == 'onnx':
            return 'YOLOv8n-face / cv2.dnn（CPU）'
        return '未加载'

    @property
    def device_label(self) -> str:
        """一句话说明"现在到底跑在哪"，给界面和自检用。"""
        if self.backend == 'ort':
            if 'CUDA' in self.active_provider:
                return 'GPU CUDA'
            if 'Dml' in self.active_provider:
                return 'GPU DirectML'
            if self.load_notes:
                return 'CPU（{}）'.format(self.load_notes[0])
            return 'CPU（onnxruntime）'
        if self.backend == 'torch':
            return 'GPU CUDA' if getattr(self, '_device', '') == 'cuda' else 'CPU（torch）'
        if self.backend == 'onnx':
            return 'CPU（cv2.dnn）'
        return '未加载'


def build_detector(kind: str = 'scrfd', conf: float = 0.35, providers=None,
                   gpu_fallback: bool = True, backend: str = 'auto'):
    """按配置挑检测器。

    kind='scrfd'（默认）：`det_500m.onnx` + onnxruntime，纯 ONNX，不依赖
        ultralytics/torch；同一段素材上与 YOLOv8n-face 有完全相同的检出率
        （775/775 帧）和框尺寸，耗时 15.7 ms vs 16.2 ms。
    其它值：仍然用 YOLOv8n-face（ort/torch/cv2 三种后端）。
    SCRFD 模型缺失或加载失败时自动退回 YOLO，不会让采集起不来。
    """
    if (kind or '').lower() in ('scrfd', 'onnx', 'onnx_scrfd'):
        try:
            from vision.face_onnx import ScrfdFaceDetector
            detector = ScrfdFaceDetector(conf=conf, providers=providers).load()
            detector.detect(__import__('numpy').zeros((240, 320, 3), dtype='uint8'))
            return detector
        except Exception as exc:                          # noqa: BLE001
            print('[视觉] SCRFD 不可用，退回 YOLOv8n-face：{}'.format(exc), flush=True)
    return FaceDetector(backend=backend, conf=conf, providers=providers,
                        gpu_fallback=gpu_fallback).load()
