# -*- coding: utf-8 -*-
"""SCRFD-500m 人脸检测（自包含实现，不依赖主程序）。

模型：det_500m.onnx（InsightFace buffalo_s 里那个），输入 640x640 BGR，
输出 9 个张量：3 个 stride × (score, bbox, kps)。kps 是 5 个点：
左眼、右眼、鼻尖、左嘴角、右嘴角。
"""
from __future__ import annotations

import cv2
import numpy as np


def _d2bbox(points, dist):
    return np.stack([points[:, 0] - dist[:, 0], points[:, 1] - dist[:, 1],
                     points[:, 0] + dist[:, 2], points[:, 1] + dist[:, 3]], axis=-1)


def _d2kps(points, dist):
    out = []
    for i in range(0, dist.shape[1], 2):
        out.append(points[:, 0] + dist[:, i])
        out.append(points[:, 1] + dist[:, i + 1])
    return np.stack(out, axis=-1)


def nms(dets, thresh=0.4):
    x1, y1, x2, y2, score = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = score.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(iou <= thresh)[0] + 1]
    return keep


class Scrfd:
    def __init__(self, model_path, input_size=(640, 640), conf=0.5, providers=None):
        import onnxruntime as ort
        providers = providers or ['CPUExecutionProvider']
        try:
            self.session = ort.InferenceSession(model_path, providers=providers)
        except Exception as exc:                           # noqa: BLE001
            # CUDA 起不来（缺 cuDNN 之类）就地退回 CPU，不让工具起不来
            print('[眼动] 推理后端 {} 不可用，退回 CPU：{}'.format(providers, exc))
            self.session = ort.InferenceSession(
                model_path, providers=['CPUExecutionProvider'])
        self.active_provider = self.session.get_providers()[0]
        self.input_name = self.session.get_inputs()[0].name
        self.size = input_size
        self.conf = conf
        self.strides = (8, 16, 32)
        self.num_anchors = 2
        self._anchor_cache = {}

    def _anchors(self, height, width):
        key = (height, width)
        if key in self._anchor_cache:
            return self._anchor_cache[key]
        out = []
        for stride in self.strides:
            hh, ww = height // stride, width // stride
            ys, xs = np.mgrid[:hh, :ww]
            centers = np.stack([xs, ys], axis=-1).astype(np.float32) * stride
            centers = np.repeat(centers.reshape(-1, 2), self.num_anchors, axis=0)
            out.append(centers)
        self._anchor_cache[key] = out
        return out

    def detect(self, image):
        """image: 灰度或 BGR。返回 [{box:(4,), kps:(5,2), score:float}]，按分数降序。"""
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        h, w = image.shape[:2]
        scale = min(self.size[0] / w, self.size[1] / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        canvas = np.zeros((self.size[1], self.size[0], 3), np.float32)
        canvas[:nh, :nw] = cv2.resize(image, (nw, nh)).astype(np.float32)
        blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]

        outs = self.session.run(None, {self.input_name: blob})
        scores = [o.reshape(-1) for o in outs[0:3]]
        boxes = [o.reshape(-1, 4) * s for o, s in zip(outs[3:6], self.strides)]
        kpss = [o.reshape(-1, 10) * s for o, s in zip(outs[6:9], self.strides)]
        anchors = self._anchors(self.size[1], self.size[0])

        dets, kps_all = [], []
        for idx in range(len(self.strides)):
            sc = np.clip(scores[idx], 0, 1)
            keep = np.where(sc >= self.conf)[0]
            if not keep.size:
                continue
            pts = anchors[idx][keep]
            box = _d2bbox(pts, boxes[idx][keep])
            kps = _d2kps(pts, kpss[idx][keep])
            dets.append(np.concatenate([box, sc[keep][:, None]], axis=1))
            kps_all.append(kps)
        if not dets:
            return []
        dets = np.concatenate(dets, axis=0)
        kps_all = np.concatenate(kps_all, axis=0)
        faces = []
        for i in nms(dets, 0.4):
            faces.append({'box': dets[i, :4] / scale,
                          'kps': kps_all[i].reshape(-1, 2) / scale,
                          'score': float(dets[i, 4])})
        faces.sort(key=lambda f: -f['score'])
        return faces

    def largest(self, faces):
        """取画面里最大的那张脸（人离镜头最近）。"""
        if not faces:
            return None
        return max(faces, key=lambda f: (f['box'][2] - f['box'][0])
                   * (f['box'][3] - f['box'][1]))
