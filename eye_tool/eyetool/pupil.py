# -*- coding: utf-8 -*-
"""瞳孔检测：阈值 + 轮廓 + 椭圆拟合。

不用深度学习：瞳孔是眼框里最暗的近似圆形区域，可见光下这条先验通常成立。
代价是对强反光、睫毛遮挡敏感——所以每个结果都带 conf，让上层决定信不信。

关键点：
* 阈值取 ROI 内的暗端分位数，不写死数值，光照变化时能自适应；
* **所有像素常数都以眼框宽度为尺度**：1080p 换成 4K 后眼框从 110 px 变成 200+ px，
  写死 5x5 的形态学核、4 px 的直径下限会让同一只眼睛量出完全不同的数；
* 有上一帧位置时优先在邻域里找，眨眼/微动时不会跳到眉毛或睫毛上。
"""
from __future__ import annotations

import cv2
import numpy as np

# 标定基准：1080p 机位下眼框约 110 px 宽，此时工作分辨率 220 px、核 5x5
_REF_ROI_W = 110.0
_REF_WORK_W = 220.0


def _odd(v: float, lo: int = 3) -> int:
    k = int(round(v))
    k = max(lo, k)
    return k if k % 2 == 1 else k + 1


def detect(gray_roi, params, last_center=None, upscale=2, last_d=None, ref_px=None):
    """返回 dict：ok / cx / cy / d_px / area / circularity / thr / conf。

    cx, cy 是相对于 gray_roi 左上角的坐标（调用方负责加回偏移）。
    ref_px：眼框宽度，用作尺度基准；不给就用 ROI 宽度。
    """
    out = {'ok': False, 'cx': float('nan'), 'cy': float('nan'),
           'd_px': float('nan'), 'area': 0.0, 'circularity': 0.0,
           'thr': 0.0, 'conf': 0.0}
    if gray_roi is None or gray_roi.size == 0:
        return out
    if gray_roi.ndim == 3:
        # 采集现在是彩色（BGR）。瞳孔判据用的是亮度，这里只对眼框这一小块
        # （几十像素见方）转灰度，1080p 全图转一次要 1.3 ms，没必要
        gray_roi = cv2.cvtColor(gray_roi, cv2.COLOR_BGR2GRAY)

    roi_w = float(gray_roi.shape[1])
    roi_h = float(gray_roi.shape[0])
    ref = float(ref_px) if ref_px else roi_w

    # 工作分辨率固定：小眼框放大（亚像素椭圆更稳），大眼框必要时**缩小**。
    # 不设上限时，人一凑近（4K 下眼框能到 500 px），形态学 + 轮廓的开销
    # 会翻倍，界面帧率从 25 掉到 15；把工作宽度压在 320 px 以内速度就稳定，
    # 而 320 仍比 1080p 机位的 220 更细。
    work_target = 320.0
    up = float(np.clip(work_target / max(roi_w, 1.0), 0.5, max(1.0, float(upscale))))
    img = cv2.resize(gray_roi, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    img = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(4, 4)).apply(img)
    blur = _odd(3.0 * up) if up > 1 else 5
    img = cv2.GaussianBlur(img, (blur, blur), 0)

    work_w = roi_w * up
    k_open = _odd(5.0 * work_w / _REF_WORK_W, 3)
    k_close = _odd(3.0 * work_w / _REF_WORK_W, 3)

    roi_area = float(img.shape[0] * img.shape[1])
    thr = float(np.percentile(img, params.dark_percentile))
    mask = (img < thr).astype(np.uint8) * 255
    # 开运算：瞳孔常被上眼睑粘出一条"尾巴"，核太小去不掉
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                      (k_open, k_open)))
    # 只开不闭：闭运算会把刚切掉的眼睑尾巴重新连回来
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                      (k_close, k_close)))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return out

    # 生理范围：瞳孔直径大约是眼框宽的 5%~34%（虹膜约 45%，超过就是连虹膜一起量了）
    d_min = max(params.min_diameter_px, 0.05 * ref)
    d_max = 0.34 * ref

    best = None
    best_score = -1.0
    fallback = None           # 不看连续性时的最优候选（连续性判据的兜底）
    fallback_score = -1.0
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        ratio = area / roi_area
        if ratio < params.min_area_ratio or ratio > params.max_area_ratio:
            continue
        peri = float(cv2.arcLength(cnt, True))
        if peri <= 1e-6 or len(cnt) < 5:
            continue
        (ex, ey), (a1, a2), _ang = cv2.fitEllipse(cnt)
        # 用椭圆的轴比做形状判据，比轮廓圆度稳：
        # 轮廓带尾巴时圆度会掉到 0.3，但椭圆本身仍然是圆的
        aspect = min(a1, a2) / max(a1, a2) if max(a1, a2) > 0 else 0.0
        if aspect < params.min_aspect:
            continue
        d = (a1 + a2) / 2.0 / up          # 换回原图像素
        if d < d_min or d > d_max:
            continue
        score = aspect * min(1.0, ratio / 0.25)
        # 直径连续性：瞳孔直径不会一帧内从 22 px 变成 10 px，
        # 偏离上一帧太多的候选（多半是眼睑缝隙、睫毛阴影）降权。
        # 但只当"优先项"：如果这一帧所有候选都离上一帧很远（人突然抬头、
        # 上一帧本身锁错了），要有兜底，否则会永久卡在错误值上——实测
        # 只在一条路径上严格要求时，有效帧率会从 85% 掉到 3%。
        rel = 0.0
        if last_d and np.isfinite(last_d) and last_d > 0:
            rel = abs(d - last_d) / max(2.0, 0.3 * last_d)
            if rel <= 0.5:
                score *= 1.0 / (1.0 + rel)
        if fallback_score < 0 or score > fallback_score:
            fallback_score = score
            fallback = (ex / up, ey / up, d, area / (up * up), aspect, thr, rel)
        # 有历史位置时，离得近的优先
        if last_center is not None and np.isfinite(last_center[0]):
            dist = ((ex / up - last_center[0]) ** 2
                    + (ey / up - last_center[1]) ** 2) ** 0.5
            if dist > max(roi_h, roi_w):
                continue
            score *= 1.0 / (1.0 + dist / (0.05 * max(roi_w, 1.0)))
        if rel > 0.5:
            continue                  # 偏离太大，不作为首选
        if score > best_score:
            best_score = score
            best = (ex / up, ey / up, d, area / (up * up), aspect, thr)

    if best is None and fallback is not None:
        best = fallback[:6]           # 兜底：这一帧确实没有连续的好候选
    if best is None:
        return out

    ex, ey, d, area, aspect, thr = best
    # 轴比越接近 1 越可信；被眼睑切掉一半时轴比会明显下降
    conf = float(min(1.0, max(0.0, (aspect - params.min_aspect)
                             / (1.0 - params.min_aspect))))
    if d > 0.9 * roi_h:
        conf *= 0.5
    out.update(ok=True, cx=float(ex), cy=float(ey), d_px=float(d),
               area=float(area), circularity=float(aspect), thr=thr, conf=conf)
    return out
