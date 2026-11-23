# -*- coding: utf-8 -*-
"""嘴部 ROI 与姿态门控。

为什么需要这个模块
------------------
张嘴分类器的输入原本是**整张人脸框**。但人脸框在低头时会把头发、眼镜、
衣领一起框进来；缩放到 100×100 后，框底那条深色横带（衣领/下巴）会被模型
当成"张开的嘴"，输出 0.86~1.00。实测（马国庆_能见度 10:34 段）：

    整脸框   中位概率 0.88，≥0.5 占 59%，最长连续 10.4 秒
    嘴部小块 中位概率 0.11，≥0.5 占 24%，最长连续 3.0 秒

真哈欠典型 3~6 秒，且 38% 的时间"整脸高但嘴部低"——说明整脸才是误判来源。
所以这里改成：**用嘴角关键点裁出嘴部小块**，并给出可见性判定；
判不出来时明确返回"嘴不可见"，而不是硬给一个概率。

关键点约定
----------
`FaceBox.keypoints` 形状 `(5, 3)`，每行 `(置信, x, y)`，顺序
`左眼、右眼、鼻尖、左嘴角、右嘴角`，坐标已在原图像素。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

# 门控原因码 → 界面上显示的中文
REASON_TEXT = {
    '': '正常',
    'no_keypoints': '无关键点',
    'low_conf': '嘴不可见',
    'head_down': '低头过度',
    'too_small': '距离过远',
    'out_of_frame': '嘴出画了',
}

MOUTH_LEFT = 3        # 关键点下标：左嘴角
MOUTH_RIGHT = 4       # 关键点下标：右嘴角


@dataclass
class GateResult:
    """一次门控的结果。"""

    roi: Tuple[int, int, int, int]   # 实际要喂分类器的矩形 (x1,y1,x2,y2)
    source: str                      # 'mouth' | 'face'
    visible: bool                    # 能否可靠判张嘴
    reason: str                      # '' | 'no_keypoints' | 'low_conf' | 'head_down' | 'too_small'
    mouth: Optional[Tuple[int, int, int, int]] = None   # 嘴部矩形，画图用

    @property
    def reason_text(self) -> str:
        return REASON_TEXT.get(self.reason, self.reason)


def _clip_rect(x1, y1, x2, y2, width: int, height: int):
    x1 = max(0, min(int(round(x1)), width))
    y1 = max(0, min(int(round(y1)), height))
    x2 = max(0, min(int(round(x2)), width))
    y2 = max(0, min(int(round(y2)), height))
    return x1, y1, x2, y2


def mouth_rect_of(box, pad_w: float = 0.85, pad_h: float = 0.70):
    """由两个嘴角算嘴部矩形；关键点不可用时返回 None。

    半宽 = 嘴角间距 × pad_w，半高 = 嘴角间距 × pad_h。
    嘴角间距 30 px 时得到约 51×42 的框，正好覆盖唇部。
    """
    kp = getattr(box, 'keypoints', None)
    if kp is None or len(kp) <= MOUTH_RIGHT:
        return None
    left, right = kp[MOUTH_LEFT], kp[MOUTH_RIGHT]
    cx = (left[1] + right[1]) / 2.0
    cy = (left[2] + right[2]) / 2.0
    span = abs(right[1] - left[1])
    if not np.isfinite([cx, cy, span]).all() or span <= 1:
        return None
    half_w = span * pad_w
    half_h = max(span * pad_h, 4.0)
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h


def gate_face(box, frame_shape, kp_conf: float = 0.5, low_ratio: float = 0.95,
              pad_w: float = 0.85, pad_h: float = 0.70, min_px: int = 12,
              enabled: bool = True) -> GateResult:
    """决定这一帧喂什么给张嘴分类器、以及能不能判。

    参数
    ----
    box : FaceBox
    frame_shape : (H, W) 或 (H, W, 3)
    kp_conf : 嘴角关键点置信下限，低于它判「嘴不可见」
    low_ratio : 嘴角纵坐标超过人脸框的这个比例 → 判「低头过度」
    pad_w / pad_h : 嘴部矩形的半宽/半高，相对嘴角间距
    min_px : 嘴部矩形最短边，太小说明人太远
    enabled : False 时退回整脸输入（旧行为）
    """
    height, width = int(frame_shape[0]), int(frame_shape[1])
    face_roi = _clip_rect(box.x1, box.y1, box.x2, box.y2, width, height)

    if not enabled:
        return GateResult(roi=face_roi, source='face', visible=True, reason='')

    kp = getattr(box, 'keypoints', None)
    if kp is None:
        # torch 后端不产关键点：退回整脸，行为与改动前一致，不误伤
        return GateResult(roi=face_roi, source='face', visible=True,
                          reason='no_keypoints')

    # ---- 1. 嘴角置信 ----
    conf = float(min(kp[MOUTH_LEFT][0], kp[MOUTH_RIGHT][0]))
    if conf < kp_conf:
        return GateResult(roi=face_roi, source='face', visible=False,
                          reason='low_conf')

    # ---- 2. 嘴是否落在画面内 ----
    # 筐会被 _clip 裁到画面里，但关键点不会。人脸从画面下缘探进来时，
    # 框高被裁小、嘴角还在画面外，比例会算出 >1 的假"低头"。
    box_h = max(float(box.y2 - box.y1), 1.0)
    mouth_y = float((kp[MOUTH_LEFT][2] + kp[MOUTH_RIGHT][2]) / 2.0)
    mouth_x = float((kp[MOUTH_LEFT][1] + kp[MOUTH_RIGHT][1]) / 2.0)
    if not (0 <= mouth_y < height - 1 and 0 <= mouth_x < width - 1):
        return GateResult(roi=face_roi, source='face', visible=False,
                          reason='out_of_frame')

    # ---- 3. 是否低头过度：嘴角贴近框底 ----
    if (mouth_y - box.y1) / box_h > low_ratio:
        return GateResult(roi=face_roi, source='face', visible=False,
                          reason='head_down')

    # ---- 4. 嘴部矩形 ----
    raw = mouth_rect_of(box, pad_w, pad_h)
    if raw is None:
        return GateResult(roi=face_roi, source='face', visible=False,
                          reason='low_conf')
    mouth = _clip_rect(*raw, width, height)
    if mouth[2] - mouth[0] < min_px or mouth[3] - mouth[1] < min_px:
        return GateResult(roi=face_roi, source='face', visible=False,
                          reason='too_small', mouth=mouth)

    return GateResult(roi=mouth, source='mouth', visible=True, reason='',
                      mouth=mouth)
