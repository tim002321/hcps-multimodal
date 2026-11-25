# -*- coding: utf-8 -*-
"""从 5 个面部关键点推出左右眼框。

SCRFD 的 5 点顺序：左眼、右眼、鼻尖、左嘴角、右嘴角。
这里不假设左右（被试左右眼在图像上的先后取决于是否镜像），
按 x 坐标排序后叫 eye_a / eye_b，避免混淆。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EyeBox:
    name: str          # 'eye_a' | 'eye_b'
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def w(self):
        return max(1, self.x2 - self.x1)

    @property
    def h(self):
        return max(1, self.y2 - self.y1)

    def as_tuple(self):
        return self.x1, self.y1, self.x2, self.y2


def eye_boxes(kps, shape, pad_w=0.85, pad_h=0.55):
    """kps: (5,2)（左眼中心、右眼中心、鼻尖、左右嘴角）；shape: (h,w[,c])。

    尺度基准是**两眼中心的间距 ipd**，不是单眼宽度：
    成人单眼（内眦到外眦）约 0.45*ipd，眼裂高约 0.22*ipd。
    所以半宽取 pad_w*单眼宽 /2，半高取 pad_h*单眼宽 /2。
    """
    h_img, w_img = shape[0], shape[1]
    pts = sorted(kps[:2], key=lambda p: p[0])
    (lx, ly), (rx, ry) = pts[0], pts[1]
    ipd = max(8.0, float(((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5))
    eye_w = 0.45 * ipd
    half_w = 0.5 * pad_w * eye_w * 1.5
    half_h = 0.5 * pad_h * eye_w * 1.5

    boxes = []
    for idx, (cx, cy) in enumerate((pts[0], pts[1])):
        x1 = int(max(0, round(cx - half_w)))
        x2 = int(min(w_img, round(cx + half_w)))
        y1 = int(max(0, round(cy - half_h)))
        y2 = int(min(h_img, round(cy + half_h)))
        if x2 - x1 < 8 or y2 - y1 < 6:
            continue
        boxes.append(EyeBox('eye_%s' % ('ab'[idx]), x1, y1, x2, y2))
    return boxes
