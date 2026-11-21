# -*- coding: utf-8 -*-
"""图像质量与头部姿态：只能在这里算，因为只有预处理阶段还看得见原图。

下游拿着 ROI 小块和坐标，永远无法补算"这一帧糊不糊、有没有过曝"。
所以即使当前不需要，也要先算出来存着。
"""
from __future__ import annotations

from typing import Dict, Tuple

import cv2
import numpy as np

from .detector import Face
from .geometry import Rect, crop

OVER_LEVEL = 245          # 判过曝的灰度阈值
UNDER_LEVEL = 15          # 判欠曝
# 拉普拉斯方差与分辨率、人脸像素数强相关，没有通用阈值。
# 这个 15 是按 640×480、脸宽约 230 px 的实测分布（7~54，中位 25）定的下限，
# 换分辨率或换机位必须重新标定，否则 is_blurry 一列没有意义。
BLUR_ALERT = 15.0


def face_quality(frame: np.ndarray, face: Face) -> Dict[str, float]:
    """在脸部矩形上算清晰度、亮度、对比度、过曝欠曝比例。

    注意是"脸部区域"而不是整帧：背景占比大时，整帧的方差会被背景主导，
    量不出人脸本身糊不糊。
    """
    rect = Rect(face.x1, face.y1, face.x2, face.y2)
    patch = crop(frame, rect)
    if patch.size == 0:
        return {}
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    std = float(gray.std())
    over = float((gray >= OVER_LEVEL).mean())
    under = float((gray <= UNDER_LEVEL).mean())
    return {
        'blur_var': round(blur, 1),
        'brightness': round(mean, 1),
        'contrast': round(std, 1),
        'over_ratio': round(over, 4),
        'under_ratio': round(under, 4),
        'is_blurry': 1 if blur < BLUR_ALERT else 0,
    }


def head_pose(pts: np.ndarray, face: Face, frame_width: int) -> Dict[str, float]:
    """用 68 点估头部滚转与偏航。

    roll  = 两眼连线相对水平的夹角（度），正数表示右侧抬高
    yaw   = 鼻尖相对两眼中点的水平偏移 / 眼距，正值表示脸转向画面右侧
    这两个量不需要 3D 模型，误差约 3~5 度，够用来筛选严重侧脸。
    """
    left = pts[36:42].mean(axis=0)
    right = pts[42:48].mean(axis=0)
    nose = pts[30]

    dx = float(right[0] - left[0])
    dy = float(right[1] - left[1])
    roll = float(np.degrees(np.arctan2(dy, dx)))
    eye_dist = max(float(np.hypot(dx, dy)), 1e-6)

    mid = (left + right) / 2.0
    yaw = float((nose[0] - mid[0]) / eye_dist)
    pitch = float((nose[1] - mid[1]) / eye_dist)

    return {
        'roll_deg': round(roll, 2),
        'yaw_ratio': round(yaw, 4),
        'pitch_ratio': round(pitch, 4),
        'face_ratio': round(face.width / max(frame_width, 1), 4),
    }


def thresholds() -> Dict[str, Tuple[float, str]]:
    """给文档和界面用的一句话说明。"""
    return {
        'blur_var': (BLUR_ALERT, '拉普拉斯方差，阈值随分辨率变化，必须按自己的数据标定'),
        'brightness': (110.0, '脸部平均灰度，建议 90~170'),
        'over_ratio': (0.02, '过曝像素占比，>2% 说明光太强'),
        'under_ratio': (0.15, '欠曝像素占比，>15% 说明太暗'),
        'roll_deg': (12.0, '眼连线倾角，>12° 说明歪头'),
        'yaw_ratio': (0.18, '鼻尖水平偏移/眼距，>0.18 说明侧脸'),
    }
