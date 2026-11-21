# -*- coding: utf-8 -*-
"""68 点可视化：分组着色 + 连线 + 可选编号。纯函数。"""
from __future__ import annotations

import cv2
import numpy as np

# (名称, 点下标, BGR 颜色, 是否闭合)
GROUPS = [
    ('jaw', list(range(0, 17)), (200, 120, 40), False),
    ('brow_l', list(range(17, 22)), (70, 170, 70), False),
    ('brow_r', list(range(22, 27)), (70, 170, 70), False),
    ('nose', list(range(27, 36)), (40, 160, 230), False),
    ('eye_l', list(range(36, 42)), (60, 60, 220), True),
    ('eye_r', list(range(42, 48)), (60, 60, 220), True),
    ('lip_out', list(range(48, 60)), (200, 70, 200), True),
    ('lip_in', list(range(60, 68)), (235, 140, 235), True),
]


def draw_landmarks(image: np.ndarray, pts: np.ndarray, show_index: bool = True,
                   thickness: int = 1) -> np.ndarray:
    out = image.copy()
    if pts is None or len(pts) != 68:
        return out
    for _name, ids, color, closed in GROUPS:
        for a, b in zip(ids[:-1], ids[1:]):
            cv2.line(out, _p(pts[a]), _p(pts[b]), color, thickness, cv2.LINE_AA)
        if closed:
            cv2.line(out, _p(pts[ids[-1]]), _p(pts[ids[0]]), color, thickness, cv2.LINE_AA)
        for i in ids:
            cv2.circle(out, _p(pts[i]), 1, color, -1, cv2.LINE_AA)
    if show_index:
        for i, (x, y) in enumerate(pts):
            if i % 2 == 0:
                cv2.putText(out, str(i), (int(x) + 2, int(y) - 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.26, (70, 70, 70), 1, cv2.LINE_AA)
    return out


def _p(point) -> tuple:
    return int(round(float(point[0]))), int(round(float(point[1])))


def caption(image: np.ndarray, text: str, height: int = 30) -> np.ndarray:
    """顶部加一条白底文字带（cv2 不支持中文，调用方传英文/数字）。"""
    out = np.full((image.shape[0] + height, image.shape[1], 3), 255, dtype=np.uint8)
    out[height:, :] = image
    cv2.putText(out, text, (8, height - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (45, 45, 45), 1, cv2.LINE_AA)
    return out
