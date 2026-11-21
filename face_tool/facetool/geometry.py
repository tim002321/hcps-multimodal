# -*- coding: utf-8 -*-
"""几何：ROI 取法、EAR、MAR、裁剪放大。全是纯函数，不碰 IO 和模型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

import cv2
import numpy as np

from .detector import Face
from .params import RunParams

LEFT_EYE = list(range(36, 42))
RIGHT_EYE = list(range(42, 48))
OUTER_LIP = list(range(48, 60))
INNER_LIP = list(range(60, 68))


@dataclass(frozen=True)
class Rect:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return self.x1, self.y1, self.x2, self.y2


def clip_rect(rect: Rect, shape: Tuple[int, int]) -> Rect:
    height, width = shape[:2]
    x1 = max(0, min(int(round(rect.x1)), width))
    y1 = max(0, min(int(round(rect.y1)), height))
    x2 = max(0, min(int(round(rect.x2)), width))
    y2 = max(0, min(int(round(rect.y2)), height))
    if x2 <= x1:
        x2 = min(width, x1 + 1)
    if y2 <= y1:
        y2 = min(height, y1 + 1)
    return Rect(x1, y1, x2, y2)


def _bbox(pts: np.ndarray, ids: Sequence[int]) -> Tuple[float, float, float, float]:
    sel = pts[list(ids)]
    return (float(sel[:, 0].min()), float(sel[:, 1].min()),
            float(sel[:, 0].max()), float(sel[:, 1].max()))


def eye_roi(pts: np.ndarray, side: str, face: Face, p: RunParams) -> Rect:
    """眼部 ROI。

    hybrid：中心取眼周 6 点包围盒中心，宽高取「脸比例」与「包围盒倍数」的较大者。
    取 max 的原因：闭眼时包围盒高只有 8 px 左右，纯按包围盒会让 ROI 逐帧塌缩。
    """
    ids = LEFT_EYE if side.upper().startswith('L') else RIGHT_EYE
    bx1, by1, bx2, by2 = _bbox(pts, ids)
    cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
    box_w, box_h = bx2 - bx1, by2 - by1

    if p.roi_mode == 'landmark_box':
        w = box_w * p.eye_box_w
        h = box_h * p.eye_box_h
    elif p.roi_mode == 'face_ratio':
        w = face.width * p.eye_w_ratio
        h = face.height * p.eye_h_ratio
    else:
        w = max(face.width * p.eye_w_ratio, box_w * p.eye_box_w)
        h = max(face.height * p.eye_h_ratio, box_h * p.eye_box_h)
    return Rect(int(round(cx - w / 2)), int(round(cy - h / 2)),
                int(round(cx + w / 2)), int(round(cy + h / 2)))


def mouth_roi(pts: np.ndarray, p: RunParams) -> Rect:
    """嘴部 ROI：外唇包围盒为中心，按倍数外扩，中心下移一点（张口时下唇向下延伸）。"""
    bx1, by1, bx2, by2 = _bbox(pts, OUTER_LIP)
    cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
    w = (bx2 - bx1) * p.mouth_w_scale
    h = (by2 - by1) * p.mouth_h_scale
    cy += h * p.mouth_dy
    return Rect(int(round(cx - w / 2)), int(round(cy - h / 2)),
                int(round(cx + w / 2)), int(round(cy + h / 2)))


def crop(image: np.ndarray, rect: Rect) -> np.ndarray:
    r = clip_rect(rect, image.shape)
    return image[r.y1:r.y2, r.x1:r.x2].copy()


def scale_roi(image: np.ndarray, factor: float) -> np.ndarray:
    """放大用最近邻：保像素原貌，方便目视核对，不引入插值假细节。"""
    if not factor or abs(factor - 1.0) < 1e-6 or image.size == 0:
        return image
    h, w = image.shape[:2]
    return cv2.resize(image, (max(1, int(w * factor)), max(1, int(h * factor))),
                      interpolation=cv2.INTER_NEAREST)


def _dist(a, b) -> float:
    return float(np.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1])))


def ear(pts: np.ndarray, ids: Sequence[int]) -> float:
    """EAR = (|p2-p6| + |p3-p5|) / (2|p1-p4|)；ids 为 6 个点下标。"""
    p1, p2, p3, p4, p5, p6 = [pts[i] for i in ids]
    denom = 2.0 * max(_dist(p1, p4), 1e-6)
    return (_dist(p2, p6) + _dist(p3, p5)) / denom


def mar_inner(pts: np.ndarray) -> float:
    """内唇张口比：|62-66| / |60-64|。"""
    return _dist(pts[62], pts[66]) / max(_dist(pts[60], pts[64]), 1e-6)


def mar_outer(pts: np.ndarray) -> float:
    """外唇张口比：|51-57| / |48-54|。"""
    return _dist(pts[51], pts[57]) / max(_dist(pts[48], pts[54]), 1e-6)


def mouth_corner_px(pts: np.ndarray) -> float:
    return _dist(pts[48], pts[54])


def eye_width_px(pts: np.ndarray) -> float:
    return (_dist(pts[36], pts[39]) + _dist(pts[42], pts[45])) / 2.0


def plausible_ear(value: float) -> bool:
    return 0.02 <= value <= 0.80


def plausible_mar(value: float) -> bool:
    """闭嘴时内唇几乎贴合，MAR_inner 可以低到 0，所以下限取 0。"""
    return 0.0 <= value <= 4.0
