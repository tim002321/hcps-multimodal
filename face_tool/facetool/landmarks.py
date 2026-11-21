# -*- coding: utf-8 -*-
"""dlib 68 点（iBUG 300-W）。

已知偏差：把人脸框换成 YOLO 的输出后，下颌 0-16 号点会比实际轮廓外扩 3~8 px
（模型是在自己的 HOG 框上训练的）。眼、眉、鼻、唇不受影响，本工具的 ROI 只用
眼和嘴，所以可以接受。
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

import cv2
import numpy as np

from .detector import Face

MODEL_NAME = 'shape_predictor_68_face_landmarks.dat'


class LandmarkEngine:
    def __init__(self, models_dir: str):
        self.model_path = os.path.join(models_dir, MODEL_NAME)
        self._predictor = None

    def load(self) -> 'LandmarkEngine':
        if not os.path.exists(self.model_path):
            raise FileNotFoundError('缺少 {}'.format(self.model_path))
        import dlib
        self._predictor = dlib.shape_predictor(self.model_path)
        return self

    @property
    def ok(self) -> bool:
        return self._predictor is not None

    def predict(self, bgr: np.ndarray, face: Face) -> Optional[np.ndarray]:
        """返回 (68,2) float32，坐标是原图像素。失败返回 None。"""
        if self._predictor is None:
            return None
        import dlib
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rect = dlib.rectangle(int(face.x1), int(face.y1), int(face.x2), int(face.y2))
        try:
            shape = self._predictor(rgb, rect)
        except Exception:                      # noqa: BLE001
            return None
        pts = np.array([[p.x, p.y] for p in shape.parts()], dtype=np.float32)
        if pts.shape != (68, 2):
            return None
        return pts


def validate(pts: Optional[np.ndarray], shape: Tuple[int, int]) -> Tuple[bool, str]:
    """粗校验：点数、越界、眼嘴顺序是否颠倒。只判断，不修改。"""
    if pts is None:
        return False, 'no_landmarks'
    if pts.shape != (68, 2):
        return False, 'bad_shape'
    if not np.isfinite(pts).all():
        return False, 'nan'
    height, width = shape[:2]
    margin = 0.25
    if pts[:, 0].min() < -width * margin or pts[:, 0].max() > width * (1 + margin):
        return False, 'x_out_of_range'
    if pts[:, 1].min() < -height * margin or pts[:, 1].max() > height * (1 + margin):
        return False, 'y_out_of_range'
    left_eye = pts[36:42].mean(axis=0)
    right_eye = pts[42:48].mean(axis=0)
    if left_eye[0] >= right_eye[0]:            # 左右眼必须左在左
        return False, 'eyes_swapped'
    mouth = pts[48:68].mean(axis=0)
    if mouth[1] <= min(left_eye[1], right_eye[1]):   # 嘴必须在眼睛下方
        return False, 'mouth_above_eyes'
    return True, 'ok'
