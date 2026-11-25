# -*- coding: utf-8 -*-
"""以虹膜为锚的瞳孔测量（MediaPipe FaceMesh）。

## 为什么换

纯阈值法在整个眼框里找"最暗的圆"，有三个躲不过的问题：
1. 眼睑阴影、睫毛、眉毛和瞳孔一样暗，谁面积大谁赢；
2. 虹膜（深棕）和瞳孔灰度差很小，阈值一松就把两个连成一片，直径虚高 2 倍；
3. 眼睑压下来时暗区变成一条缝，椭圆的轴比一掉就被判废。

FaceMesh 给的是**结构**，不是灰度：虹膜中心 + 半径（468~472 / 473~477）、
上下眼睑轮廓。有了虹膜就能把搜索范围压到"虹膜里面"，噪声根本进不来；
而且虹膜物理直径近似常数（成人 11.7 mm），可以直接换算成毫米，
不受人离相机远近的影响。

## 做法

```
FaceMesh(人脸裁剪) → 虹膜中心 c0、半径 r0、眼睑开合比
        ↓
在 c0 周围 0.9*r0 的圆内做自适应阈值，取包含中心的那块
        ↓
椭圆拟合 → 直径 d_px；同时给 d_px / (2*r0) 这个无量纲比值
```

比值才是能跨帧、跨人比较的量：0.2 表示瞳孔占虹膜 20%，成人典型 0.2~0.5。

## 中文路径的坑

MediaPipe 的 C++ 层用窄字符路径打开 .binarypb，路径里有中文就报
"The path does not exist"。所以这里先看包是不是在 ASCII 路径下，
不是的话用一个目录连接（junction）绕过去。这和本机 Qt 插件那个问题同源。
"""
from __future__ import annotations

import os
import subprocess
import sys

import cv2
import numpy as np

# MediaPipe FaceMesh 的索引约定
IRIS_L = (468, 469, 470, 471, 472)      # 左虹膜：中心 + 上下左右 4 点
IRIS_R = (473, 474, 475, 476, 477)
EYE_L = (33, 133, 159, 145)             # 外眦、内眦、上睑、下睑
EYE_R = (362, 263, 386, 374)

IRIS_MM = 11.7                          # 成人虹膜横径（mm），国际通用近似值

_ASCII_ROOT = os.path.join(os.environ.get('LOCALAPPDATA', r'C:\mp_ascii'), 'mp_ascii')


def _is_ascii(path: str) -> bool:
    try:
        path.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def _prepare_ascii_root():
    """在 **import mediapipe 之前** 决定从哪个路径导入，返回需要插入的根目录。

    必须先决定、后导入：如果先从中文路径 import 一份，再从 ASCII 路径 import
    另一份，protobuf 会报 `type "Image" is already registered`（同一套
    .proto 在进程里注册两次）。所以这里只用 find_spec 查位置，不执行模块。
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec('mediapipe')
    except Exception:                                      # noqa: BLE001
        return None
    if spec is None or not spec.origin:
        return None
    here = os.path.dirname(os.path.abspath(spec.origin))
    if _is_ascii(here):
        return None                       # 本来就是 ASCII 路径，什么都不用做

    link = os.path.join(_ASCII_ROOT, 'mediapipe')
    if not os.path.isdir(link):
        try:
            os.makedirs(_ASCII_ROOT, exist_ok=True)
            # 目录连接不需要管理员权限；C++ 层拿到的是纯 ASCII 路径
            subprocess.run(['cmd', '/c', 'mklink', '/J', link, here],
                           capture_output=True, timeout=15, check=False)
        except Exception:                                  # noqa: BLE001
            return None
    if not os.path.isdir(os.path.join(link, 'modules')):
        return None
    if _ASCII_ROOT in sys.path:
        sys.path.remove(_ASCII_ROOT)
    sys.path.insert(0, _ASCII_ROOT)
    return _ASCII_ROOT


class IrisTracker:
    """一帧 → 左右眼的虹膜几何 + 瞳孔直径。"""

    def __init__(self, static: bool = False, refine: bool = True,
                 min_conf: float = 0.4):
        self.available = False
        self.error = ''
        self._mesh = None
        self._static = static
        self._refine = refine
        self._conf = min_conf
        self._last_center = {}

    def load(self):
        if self._mesh is not None:
            return self
        root = _prepare_ascii_root()          # 先定路径，再 import
        try:
            import mediapipe as mp
            if root is None and not _is_ascii(os.path.dirname(os.path.abspath(mp.__file__))):
                raise RuntimeError('mediapipe 仍在中文字路径下，且目录连接创建失败')
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=self._static, max_num_faces=1,
                refine_landmarks=self._refine,
                min_detection_confidence=self._conf,
                min_tracking_confidence=0.4)
            self.available = True
        except Exception as exc:                           # noqa: BLE001
            self.error = str(exc)
            self._mesh = None
        return self

    # ------------------------------------------------------------------ 主入口
    def process(self, frame_bgr, face_box=None, pad: float = 0.25):
        """返回 {'ok', 'eyes': {L/R: {...}}, 'latency_ms'}。

        给了 face_box 就只在那块（外扩 pad）上跑，4K 全图直接喂会慢 3~4 倍。
        """
        out = {'ok': False, 'eyes': {}, 'latency_ms': 0.0}
        if self._mesh is None and not self.load():
            return out
        h, w = frame_bgr.shape[:2]
        if face_box is not None:
            x1, y1, x2, y2 = [float(v) for v in face_box]
            dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
            a = int(max(0, x1 - dx)); b = int(max(0, y1 - dy))
            c = int(min(w, x2 + dx)); d = int(min(h, y2 + dy))
        else:
            a = b = 0; c, d = w, h
        crop = frame_bgr[b:d, a:c]
        if crop.size == 0:
            return out

        import time
        t0 = time.perf_counter()
        res = self._mesh.process(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        out['latency_ms'] = (time.perf_counter() - t0) * 1000.0
        if not res.multi_face_landmarks:
            return out

        lm = res.multi_face_landmarks[0].landmark
        ch, cw = crop.shape[:2]
        pts = np.array([[p.x * cw + a, p.y * ch + b] for p in lm], dtype=np.float32)

        for side, iris_idx, eye_idx in (('L', IRIS_L, EYE_L), ('R', IRIS_R, EYE_R)):
            ring = pts[list(iris_idx)]
            center = ring.mean(axis=0)
            r_iris = float(np.mean(np.linalg.norm(ring - center, axis=1)))
            if r_iris < 3:
                continue
            outer, inner, upper, lower = [pts[i] for i in eye_idx]
            eye_w = float(np.linalg.norm(outer - inner))
            eye_h = float(np.linalg.norm(upper - lower))
            info = {
                'cx': float(center[0]), 'cy': float(center[1]),
                'r_iris': r_iris, 'd_iris': 2 * r_iris,
                'open_ratio': eye_h / max(eye_w, 1e-6),
                'eye_w': eye_w,
            }
            info.update(self._pupil_in_iris(frame_bgr, center, r_iris, side))
            out['eyes'][side] = info
        out['ok'] = bool(out['eyes'])
        return out

    # ------------------------------------------------------- 虹膜内部找瞳孔
    def _pupil_in_iris(self, frame_bgr, center, r_iris, side):
        """只在虹膜圆内找最暗的连通块，返回直径与比值。

        范围被虹膜半径限制住之后，眼睑阴影、睫毛、眉毛都在外面，
        剩下的竞争只发生在"瞳孔 vs 虹膜"，而瞳孔一定更暗且更靠中心。
        """
        res = {'d_pupil': float('nan'), 'ratio': float('nan'),
               'conf': 0.0, 'pupil_ok': False}
        h, w = frame_bgr.shape[:2]
        cx, cy = float(center[0]), float(center[1])
        rad = int(round(r_iris * 1.05))
        a = max(0, int(cx) - rad); b = max(0, int(cy) - rad)
        c = min(w, int(cx) + rad); d = min(h, int(cy) + rad)
        if c - a < 8 or d - b < 8:
            return res

        roi = frame_bgr[b:d, a:c]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        # 只取虹膜圆内部：外面一律置白，避免眼白/皮肤参与分位数
        yy, xx = np.mgrid[0:gray.shape[0], 0:gray.shape[1]]
        mask_circle = ((xx + a - cx) ** 2 + (yy + b - cy) ** 2) <= (r_iris * 0.95) ** 2
        if mask_circle.sum() < 30:
            return res
        vals = gray[mask_circle]
        # 虹膜内最暗的 30% 作为候选（瞳孔+深色虹膜的暗部）
        thr = float(np.percentile(vals, 30))
        mask = ((gray <= thr) & mask_circle).astype(np.uint8) * 255
        k = max(3, int(round(r_iris * 0.12)) | 1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))

        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return res
        px, py = cx - a, cy - b
        # 优先取包含虹膜中心的那块；没有就取离中心最近的
        best = None
        for i in range(1, n):
            area = float(stats[i, cv2.CC_STAT_AREA])
            if area < 20:
                continue
            dist = float(np.hypot(cents[i][0] - px, cents[i][1] - py))
            if dist > r_iris * 0.55:
                continue
            score = area / (1.0 + dist / max(r_iris * 0.15, 1.0))
            if best is None or score > best[0]:
                best = (score, i, area, cents[i])
        if best is None:
            return res

        _score, idx, area, (bx, by) = best
        comp = (labels == idx).astype(np.uint8)
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cnts or len(cnts[0]) < 5:
            return res
        (ex, ey), (a1, a2), _ang = cv2.fitEllipse(cnts[0])
        d_ell = (a1 + a2) / 2.0
        d_area = 2.0 * np.sqrt(area / np.pi)
        d = 0.5 * (d_ell + d_area)          # 两种估计取平均，抗锯齿边缘更稳
        aspect = min(a1, a2) / max(a1, a2) if max(a1, a2) > 0 else 0.0

        res['d_pupil'] = float(d)
        res['ratio'] = float(d / (2 * r_iris))
        # 置信度：形状越圆、越靠近虹膜中心越可信
        center_off = float(np.hypot(ex - px, ey - py)) / max(r_iris, 1e-6)
        res['conf'] = float(max(0.0, min(1.0, aspect)) * max(0.0, 1.0 - center_off))
        res['pupil_ok'] = bool(0.05 <= res['ratio'] <= 0.75 and aspect >= 0.4)
        return res


def iris_mm(d_px: float, d_iris_px: float) -> float:
    """按虹膜 11.7 mm 折算瞳孔物理直径（mm）。"""
    if not d_iris_px or not np.isfinite(d_px):
        return float('nan')
    return float(d_px) * IRIS_MM / float(d_iris_px)
