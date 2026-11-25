# -*- coding: utf-8 -*-
"""指标汇总：瞳孔基线、相对变化、闭眼比例。"""
from __future__ import annotations

from collections import deque

import numpy as np


class EyeMetrics:
    """基线用「最近 N 个有效瞳孔直径的 80 分位」估计。

    为什么不用均值/中位数：眨眼时瞳孔消失或骤缩，均值会被拖低；
    80 分位基本落在"睁眼"那一侧，长时间闭眼也不会失准。
    """

    def __init__(self, window=240, closed_ratio=0.45):
        self.samples = deque(maxlen=window)     # (t, d_px)
        self.closed_ratio = closed_ratio
        self.frames = 0
        self.valid_frames = 0
        self.closed_frames = 0
        self._last_d = float('nan')

    def update(self, t, d_px, found):
        self.frames += 1
        if found and np.isfinite(d_px) and d_px > 0:
            self.samples.append((t, d_px))
            self._last_d = d_px
            self.valid_frames += 1

    @property
    def baseline(self):
        if len(self.samples) < 8:
            return float('nan')
        vals = np.array([d for _, d in self.samples], dtype=float)
        return float(np.percentile(vals, 80))

    def is_closed(self, d_px, found):
        b = self.baseline
        if not np.isfinite(b) or b <= 0:
            return False
        if not found or not np.isfinite(d_px) or d_px <= 0:
            return True
        return d_px < self.closed_ratio * b

    def note_closed(self, closed):
        if closed:
            self.closed_frames += 1

    def delta_pct(self, d_px, found):
        b = self.baseline
        if not found or not np.isfinite(d_px) or not np.isfinite(b) or b <= 0:
            return float('nan')
        return (d_px - b) / b * 100.0

    def validity(self):
        if not self.frames:
            return 0.0
        return self.valid_frames / float(self.frames)

    def closed_fraction(self):
        if not self.frames:
            return 0.0
        return self.closed_frames / float(self.frames)
