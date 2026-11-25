# -*- coding: utf-8 -*-
"""核心流程：一帧 → 人脸 → 眼框 → 瞳孔 → 眨眼。与输入源无关。"""
from __future__ import annotations

import cv2
import numpy as np

from .blink import BlinkDetector
from .eye_roi import eye_boxes
from .metrics import EyeMetrics
from .pupil import detect as detect_pupil
from .iris_pupil import IrisTracker, iris_mm
from .scrfd import Scrfd


class EyePipeline:
    def __init__(self, params):
        self.p = params
        self.detector = Scrfd(params.detect.face_model,
                              conf=params.detect.face_conf,
                              providers=params.detect.providers)
        self.blink = BlinkDetector(min_duration_ms=params.blink.min_duration_ms,
                                   max_duration_ms=params.blink.max_duration_ms,
                                   merge_gap_ms=params.blink.merge_gap_ms)
        self.metrics = EyeMetrics(closed_ratio=params.blink.close_ratio)
        self._last_center = {'eye_a': None, 'eye_b': None}
        self._last_face = None
        self._last_d = {'eye_a': None, 'eye_b': None}
        self._missing_run = 0
        self._closed_run = 0
        self._master = None          # 锁定的主眼（左右）
        self._master_miss = 0
        self._w_hist = []            # 眼框宽历史，用于把直径折算到统一的尺度
        self._d_hist = []            # 最近几帧的直径，做 5 点中值去单帧尖峰
        self._open_hist = []         # 眼睑开合比历史，用来估"本人睁眼基线"
        self._open_recent = []       # 最近 3 帧，做中值滤掉单帧跳变
        self._eyes_closed = False    # 带迟滞的闭眼状态
        self._n = 0
        self.detect_interval = max(1, int(getattr(params.detect, 'interval', 2)))
        # 虹膜锚定法：能用就优先用（阈值法只在退路里）
        self.iris = None
        if getattr(params.detect, 'use_iris', True):
            self.iris = IrisTracker(static=False).load()
            if not self.iris.available:
                print('[眼动] 虹膜模型不可用，退回阈值法：{}'.format(self.iris.error[:120]),
                      flush=True)
                self.iris = None

    def process(self, frame, t):
        """frame: 灰度或 BGR。返回结果字典（含供标注的几何信息）。"""
        res = {'face_ok': False, 'boxes': [], 'eyes': {}, 'closed': False,
               'd_mean': float('nan'), 'baseline': float('nan'),
               'delta_pct': float('nan'), 'bpm': float('nan'),
               'blinks': self.blink.count, 'blink_event': None,
               'face_box': None, 'kps': None}

        self._n += 1
        # 人脸位置变化慢：每隔 interval 帧检测一次，中间帧复用上一次的结果，
        # 把 1080p 下 ~20 ms 的检测开销摊薄到两帧
        if self._last_face is None or self._n % self.detect_interval == 0:
            faces = self.detector.detect(frame)
            self._last_face = self.detector.largest(faces)
        face = self._last_face
        if face is None:
            self.metrics.update(t, float('nan'), False)
            return res

        res['face_ok'] = True
        res['face_box'] = face['box']
        res['kps'] = face['kps']
        boxes = eye_boxes(face['kps'], frame.shape,
                          pad_w=self.p.detect.eye_pad_w,
                          pad_h=self.p.detect.eye_pad_h)
        res['boxes'] = boxes

        if self.iris is not None:
            ir = self.iris.process(frame, face['box'])
            if ir.get('ok'):
                return self._finish_iris(res, ir, t)

        diameters, weights = [], []
        for box in boxes:
            x1, y1, x2, y2 = box.as_tuple()
            roi = frame[y1:y2, x1:x2]
            last = self._last_center.get(box.name) if self.p.pupil.use_last_center else None
            pr = detect_pupil(roi, self.p.pupil, last,
                              upscale=self.p.detect.upscale,
                              last_d=self._last_d.get(box.name),
                              ref_px=box.w)      # 尺度基准：4K 下的直径比 1080p 大 4 倍
            if pr['ok']:
                # 记住的是 ROI 局部坐标（下一帧比较也用这个坐标系），
                # 输出前再加回偏移 —— 两者混用会让跟踪判据全部失效
                self._last_center[box.name] = (pr['cx'], pr['cy'])
                self._last_d[box.name] = pr['d_px']
                pr['cx'] += x1
                pr['cy'] += y1
            res['eyes'][box.name] = pr
            res['eyes'][box.name]['roi'] = box.as_tuple()
            res['eyes'][box.name]['eye_w'] = box.w
            if pr['ok']:
                diameters.append(pr['d_px'])
                weights.append(max(pr['conf'], 1e-3))

        # 两眼瞳孔应当一样大（生理差异 <1 mm）。但两只眼受眼睑遮挡程度不同，
        # 阈值法给出的直径能差 30% 以上；如果按权重平均，某只眼一失效，
        # 输出就会在 40↔56 之间跳。所以固定用"分辨率更高那只眼"做主眼
        # （眼框更宽 = 离相机更近、透视变形更小），另一只只做一致性校验。
        d_mean = float('nan')
        found = False
        valid = [(name, v) for name, v in res['eyes'].items() if v['ok']]
        if valid:
            # 主眼一旦选定就锁住：两只眼量出的直径能差 20~30%（眼睑遮挡程度不同），
            # 按帧挑"更宽的那只"会让人脸微动时输出在两只眼的值之间来回跳。
            # 锁定的那只这一帧失效时，先用另一只顶着（不能输出空值），
            # 连续失效 3 帧才真正换主眼。
            eyes = res['eyes']
            locked = self._master
            if locked and eyes.get(locked, {}).get('ok'):
                self._master_miss = 0
                master_name = locked
            else:
                self._master_miss += 1
                valid.sort(key=lambda kv: kv[1].get('eye_w', 0), reverse=True)
                master_name = valid[0][0]
                if locked is None or self._master_miss >= 3 or locked not in eyes:
                    self._master = master_name
                    self._master_miss = 0
            master = eyes[master_name]
            others = [v['d_px'] for n, v in valid if n != master_name]
            d_master = master['d_px']
            if others and abs(np.mean(others) - d_master) <= 0.25 * d_master:
                # 两眼一致，平均一下更抗噪
                d_mean = float(np.mean([d_master] + others))
            else:
                d_mean = float(d_master)
            # 距离归一：人往前坐 20 cm，眼框和瞳孔都会按同比例变大，
            # 直接报像素值就会被当成"瞳孔放大"。用眼框宽做尺度基准，
            # 把直径折算到"眼框宽 = 历史中位数"的那个距离上，
            # 之后人怎么动，读数都只反映瞳孔本身的变化。
            eyw = float(master.get('eye_w') or 0)
            if eyw > 1:
                self._w_hist.append(eyw)
                if len(self._w_hist) > 300:
                    del self._w_hist[:-300]
                ref_w = float(np.median(self._w_hist))
                d_raw = d_mean
                d_mean = d_mean * ref_w / eyw
                res['d_raw'] = d_raw
                res['eye_w_ref'] = ref_w
            # 5 点中值：阈值法偶尔会漏进一点眼睑阴影，单帧读数会掉 30~50%。
            # 瞳孔是慢变量（几秒才动一次），中值滤波的 2 帧延迟（25fps 下 80 ms）
            # 对趋势分析没有影响，但能把尖峰去掉。
            self._d_hist.append(d_mean)
            if len(self._d_hist) > 5:
                del self._d_hist[:-5]
            res['d_raw'] = res.get('d_raw', d_mean)
            d_mean = float(np.median(self._d_hist))
            found = True
            res['master_eye'] = master_name

        self.metrics.update(t, d_mean, found)

        # 闭眼判定加两处保护，否则检测抖动会被当成眨眼：
        #   1. 瞳孔丢失要连续 3 帧才算闭眼（单帧丢失是噪声）
        #   2. 直径低于阈值要连续 2 帧（避免一帧跳变）
        self._missing_run = 0 if found else self._missing_run + 1
        shrink = found and np.isfinite(self.metrics.baseline) \
            and d_mean < self.p.blink.close_ratio * self.metrics.baseline
        self._closed_run = self._closed_run + 1 if shrink else 0
        closed = (self._missing_run >= 3) or (self._closed_run >= 2)
        self.metrics.note_closed(closed)

        ev = self.blink.update(t, closed)
        res.update(closed=closed, d_mean=d_mean, blink_event=ev,
                   blinks=self.blink.count,
                   baseline=self.metrics.baseline,
                   delta_pct=self.metrics.delta_pct(d_mean, found),
                   bpm=self.blink.rate_per_min(t))
        return res


    # ------------------------------------------------------------ 虹膜锚定分支
    def _finish_iris(self, res, ir, t):
        """把 MediaPipe 的虹膜几何变成和阈值法一致的输出结构。

        关键区别：直径以 **毫米** 为单位（按虹膜 11.7 mm 折算），
        所以换个距离、换个分辨率，数值都不变，不用再做距离归一。
        """
        eyes = {}
        for side, e in ir['eyes'].items():
            ok = bool(e.get('pupil_ok'))
            d_mm = iris_mm(e['d_pupil'], e['d_iris']) if ok else float('nan')
            r = float(e['r_iris'])
            eyes[side] = {
                'ok': ok,
                'cx': float(e['cx']), 'cy': float(e['cy']),
                'd_px': float(e['d_pupil']),
                'd_iris': float(e['d_iris']),
                'ratio': float(e['ratio']),
                'open_ratio': float(e['open_ratio']),
                'd_mm': d_mm,
                'conf': float(e['conf']),
                'eye_w': float(e['eye_w']),
                'roi': (int(e['cx'] - r), int(e['cy'] - r),
                        int(e['cx'] + r), int(e['cy'] + r)),
                'source': 'iris',
            }
        res['eyes'] = eyes
        res['unit'] = 'mm'
        # 眼框改用虹膜几何来画：SCRFD 的 5 个点在 4K 下会偏低半个框高，
        # 画出来的框盖住眉毛和脸颊，看上去像"框错了眼"
        from .eye_roi import EyeBox
        boxes = []
        order = sorted(eyes.items(), key=lambda kv: kv[1]['cx'])
        for name, (side, e) in enumerate(order):
            half_w = max(6.0, e['d_iris'] * 0.95)
            half_h = max(4.0, e['d_iris'] * 0.55)
            boxes.append(EyeBox(name='eye_a' if name == 0 else 'eye_b',
                                x1=int(e['cx'] - half_w), y1=int(e['cy'] - half_h),
                                x2=int(e['cx'] + half_w), y2=int(e['cy'] + half_h)))
        if boxes:
            res['boxes'] = boxes
        res['iris_latency_ms'] = ir.get('latency_ms', 0.0)

        valid = [(n, v) for n, v in eyes.items() if v['ok']]
        d_mean = float('nan')
        found = False
        if valid:
            eyes_ = eyes
            locked = self._master
            if locked and eyes_.get(locked, {}).get('ok'):
                self._master_miss = 0
                master_name = locked
            else:
                self._master_miss += 1
                valid.sort(key=lambda kv: kv[1].get('d_iris', 0), reverse=True)
                master_name = valid[0][0]
                if locked is None or self._master_miss >= 3 or locked not in eyes_:
                    self._master = master_name
                    self._master_miss = 0
            d_mean = float(eyes[master_name]['d_mm'])
            others = [v['d_mm'] for n, v in valid if n != master_name]
            if others and abs(np.mean(others) - d_mean) <= 0.25 * max(d_mean, 1e-6):
                d_mean = float(np.mean([d_mean] + others))
            # 5 点中值：虹膜法已经很稳，这一层只用来抹掉偶发的边缘误判
            self._d_hist.append(d_mean)
            if len(self._d_hist) > 5:
                del self._d_hist[:-5]
            d_mean = float(np.median(self._d_hist))
            res['ratio_mean'] = float(np.median([v['ratio'] for v in eyes.values()
                                                 if v['ok']]))
            found = True
            res['master_eye'] = master_name
        res['d_mean'] = d_mean

        self.metrics.update(t, d_mean, found)

        # 闭眼判定改用**眼睑开合比**：瞳孔被眼皮盖住前，上下眼睑先合拢，
        # 这个信号比"瞳孔消失"早 30~60 ms，而且不会被眼影、睫毛骗到。
        # 闭眼判定：用"相对本人基线的眼睑开合比"，并加迟滞。
        # 之前的写法有两个致命问题：
        #   ① 要求连续 2 帧低于阈值才算闭眼，把 2 帧眨眼的第 1 帧吃掉了，
        #      30fps 下几乎每个眨眼都只剩 1 帧；
        #   ② 阈值写死 0.19，而这个人睁眼基线是 0.35，眨眼时并不一定掉到 0.19 以下。
        # 现在：低于 基线×0.55 立即进入闭眼，回到 基线×0.75 才算睁开，
        # 迟滞只防抖动，不再吞掉起始帧。
        opens = [v['open_ratio'] for v in eyes.values() if v.get('open_ratio') is not None]
        closed = False
        if opens:
            o = float(min(opens))
            # 3 点中值：关键点偶发跳变会让单帧掉到 0.0（下一帧又回到 0.3），
            # 这在生理上不可能——眼睑动不了那么快，直接当噪声抹掉
            self._open_recent.append(o)
            if len(self._open_recent) > 3:
                del self._open_recent[:-3]
            o = float(np.median(self._open_recent))
            self._open_hist.append(o)
            if len(self._open_hist) > 300:
                del self._open_hist[:-300]
            base = float(np.percentile(self._open_hist, 90))     # 本人睁眼基线
            enter = float(getattr(self.p.blink, 'open_ratio_enter', 0.55)) * base
            leave = float(getattr(self.p.blink, 'open_ratio_exit', 0.75)) * base
            if self._eyes_closed:
                self._eyes_closed = o < leave
            else:
                self._eyes_closed = o < enter
            closed = self._eyes_closed
        # 瞳孔丢失（人脸侧转、出画）**不再算闭眼**：那是"测不到"，
        # 当成闭眼会凭空造出眨眼事件。只在连续丢失时把结果标成无效。
        self._missing_run = 0 if found else self._missing_run + 1
        if not opens:
            closed = self._eyes_closed          # 保持上一帧状态，不新增事件
        self.metrics.note_closed(closed)

        ev = self.blink.update(t, closed)
        res.update(closed=closed, blink_event=ev, blinks=self.blink.count,
                   baseline=self.metrics.baseline,
                   delta_pct=self.metrics.delta_pct(d_mean, found),
                   bpm=self.blink.rate_per_min(t))
        return res


def _interp(scale):
    """缩小倍数大时用 INTER_LINEAR：4K→960 用 INTER_AREA 要 18 ms，
    LINEAR 只要 4 ms，预览里看不出差别。"""
    return cv2.INTER_AREA if scale > 0.6 else cv2.INTER_LINEAR


def annotate(frame, res, hud_lines=(), size=None):
    """把结果画到 BGR 图上（灰度和彩色输入都支持）。

    size=(w,h) 时先缩放再画：1080p 彩色整图 copy 一次就是 6 MB，
    在预览里只需要 960x540 —— 先缩后画，界面帧率从 27 提到 50 以上。
    """
    if frame.ndim == 2:
        vis = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        sx = sy = 1.0
    else:
        sx = sy = 1.0
        vis = None
    if size is not None:
        tw, th = int(size[0]), int(size[1])
    else:
        tw, th = frame.shape[1], frame.shape[0]
    if vis is None:
        if (tw, th) == (frame.shape[1], frame.shape[0]):
            vis = frame.copy()
        else:
            sx = tw / float(frame.shape[1])
            sy = th / float(frame.shape[0])
            vis = cv2.resize(frame, (tw, th), interpolation=_interp(sx))
    elif (tw, th) != (frame.shape[1], frame.shape[0]):
        sx = tw / float(frame.shape[1])
        sy = th / float(frame.shape[0])
        vis = cv2.resize(vis, (tw, th), interpolation=_interp(sx))

    def pt(x, y):
        return (int(round(x * sx)), int(round(y * sy)))

    if res.get('face_box') is not None:
        x1, y1, x2, y2 = [float(v) for v in res['face_box']]
        cv2.rectangle(vis, pt(x1, y1), pt(x2, y2), (120, 120, 120), 1)
    for box in res.get('boxes', []):
        x1, y1, x2, y2 = [float(v) for v in box.as_tuple()]
        cv2.rectangle(vis, pt(x1, y1), pt(x2, y2), (60, 160, 220), 1)

    fs = max(0.32, 0.35 * sx)          # 字随画幅缩放，预览里才读得清
    for name, pr in res.get('eyes', {}).items():
        if pr['ok']:
            center = pt(pr['cx'], pr['cy'])
            r = int(round(pr['d_px'] / 2 * sx))
            cv2.ellipse(vis, center, (max(1, r), max(1, r)), 0, 0, 360, (60, 60, 230), 1)
            cv2.putText(vis, '%.1fpx' % pr['d_px'],
                        (center[0] + r + 3, center[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, fs, (60, 60, 230), 1)
        else:
            x1, y1, x2, y2 = pr['roi']
            cv2.putText(vis, 'x', pt(x1 + 3, y1 + 12),
                        cv2.FONT_HERSHEY_SIMPLEX, fs * 1.3, (60, 60, 200), 1)

    # cv2.putText 只认 ASCII：中文会渲染成方块，HUD 一律用英文/数字
    scale = max(0.5, vis.shape[1] / 1280.0 * 0.8)
    thick = max(1, int(round(scale * 2)))
    step = int(round(26 * scale))
    y = step
    for line in hud_lines:
        line = ''.join(ch if ord(ch) < 128 else '?' for ch in str(line))
        cv2.putText(vis, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (30, 30, 30), thick + 1, cv2.LINE_AA)
        cv2.putText(vis, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (245, 245, 245), thick, cv2.LINE_AA)
        y += step
    return vis


def draw_hud(vis, lines, scale=None):
    """在（可能已缩小的）画面上写 HUD，字号按画面宽度自适应。

    和 annotate 分开，是因为预览要先缩小再写字，否则字会跟着缩一半。
    """
    if not lines:
        return vis
    scale = scale or max(0.5, vis.shape[1] / 1280.0 * 0.8)
    thick = max(1, int(round(scale * 2)))
    step = int(round(26 * scale))
    y = step
    for line in lines:
        line = ''.join(ch if ord(ch) < 128 else '?' for ch in str(line))
        cv2.putText(vis, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (25, 25, 25), thick + 2, cv2.LINE_AA)
        cv2.putText(vis, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (250, 250, 250), thick, cv2.LINE_AA)
        y += step
    return vis
