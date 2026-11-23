# -*- coding: utf-8 -*-
"""SCRFD 人脸检测 + 2d106det 关键点，纯 onnxruntime（不依赖 ultralytics/YOLO）。"""
import cv2
import numpy as np
import onnxruntime as ort

ARCFACE_SRC = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)


def _distance2bbox(points, distance):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    out = []
    for i in range(0, distance.shape[1], 2):
        out.append(points[:, 0] + distance[:, i])
        out.append(points[:, 1] + distance[:, i + 1])
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


class ScrfdDetector:
    """SCRFD 500m：输入 BGR 图，输出 [{box, kps(5,2), score}]。"""

    def __init__(self, model_path, input_size=(640, 640), conf=0.5, providers=None):
        self.session = ort.InferenceSession(model_path,
                                            providers=providers or ['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name
        self.size = input_size
        self.conf = conf
        self.strides = (8, 16, 32)
        self.num_anchors = 2

    def _anchors(self, height, width):
        out = []
        for stride in self.strides:
            hh, ww = height // stride, width // stride
            ys, xs = np.mgrid[:hh, :ww]
            centers = np.stack([xs, ys], axis=-1).astype(np.float32) * stride
            centers = np.repeat(centers.reshape(-1, 2), self.num_anchors, axis=0)
            out.append(centers)
        return out

    def detect(self, image_bgr):
        h, w = image_bgr.shape[:2]
        # 等比缩放到 input_size，保持长宽比（SCRFD 对拉伸不敏感，但等比更稳）
        scale = min(self.size[0] / w, self.size[1] / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(image_bgr, (nw, nh))
        canvas = np.zeros((self.size[1], self.size[0], 3), np.float32)
        canvas[:nh, :nw] = resized.astype(np.float32)

        blob = (canvas - 127.5) / 128.0
        blob = blob.transpose(2, 0, 1)[None]
        outs = self.session.run(None, {self.input_name: blob})
        scores = [o.reshape(-1) for o in outs[0:3]]
        bboxes = [o.reshape(-1, 4) * s for o, s in zip(outs[3:6], self.strides)]
        kpss = [o.reshape(-1, 10) * s for o, s in zip(outs[6:9], self.strides)]

        anchors = self._anchors(self.size[1], self.size[0])
        dets, kps_all = [], []
        for idx, stride in enumerate(self.strides):
            sc = np.clip(scores[idx], 0, 1)
            keep = np.where(sc >= self.conf)[0]
            if not keep.size:
                continue
            pts = anchors[idx][keep]
            box = _distance2bbox(pts, bboxes[idx][keep])
            kps = _distance2kps(pts, kpss[idx][keep])
            dets.append(np.concatenate([box, sc[keep][:, None]], axis=1))
            kps_all.append(kps)
        if not dets:
            return []
        dets = np.concatenate(dets, axis=0)
        kps_all = np.concatenate(kps_all, axis=0)
        keep = nms(dets, 0.4)

        faces = []
        for i in keep:
            box = dets[i, :4] / scale
            kps = kps_all[i].reshape(-1, 2) / scale
            faces.append({'box': box, 'kps': kps, 'score': float(dets[i, 4])})
        faces.sort(key=lambda f: -f['score'])
        return faces


class Landmark106:
    """2d106det：输入对齐后的 192×192 人脸，输出 106 个点（原图坐标）。"""

    def __init__(self, model_path, providers=None):
        self.session = ort.InferenceSession(model_path,
                                            providers=providers or ['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name

    def __call__(self, image_bgr, kps5):
        size = 192
        dst = ARCFACE_SRC * (size / 112.0)
        matrix, _ = cv2.estimateAffinePartial2D(np.asarray(kps5, np.float32), dst,
                                                method=cv2.LMEDS)
        if matrix is None:
            return None
        aligned = cv2.warpAffine(image_bgr, matrix, (size, size), borderValue=0.0)
        blob = aligned.astype(np.float32).transpose(2, 0, 1)[None]
        out = self.session.run(None, {self.input_name: blob})[0].reshape(-1, 2)
        # 这个 2d106det 输出的是 [-1, 1] 的归一化坐标（不是常见的 0~192 像素），
        # 先映射回 192×192 再反变换回原图。
        pts_aligned = (out + 1.0) * (size / 2.0)
        inverse = cv2.invertAffineTransform(matrix)
        pts = cv2.transform(pts_aligned[None].astype(np.float32), inverse)[0]
        return pts


class ScrfdFaceDetector:
    """和 vision.face_detector.FaceDetector 同接口（detect(frame) → [FaceBox]）。

    纯 onnxruntime，不依赖 ultralytics/torch。除人脸框外还给 5 个关键点，
    可直接喂给现有的 pose_gate 和嘴部裁剪。
    """

    def __init__(self, model_path=None, conf: float = 0.35, providers=None,
                 input_size=(640, 640)):
        if model_path is None:
            from core.paths import resolve_resource_dir
            model_path = '%s/det_500m.onnx' % resolve_resource_dir('models')
        self.model_path = model_path
        self.conf = float(conf)
        self.providers = list(providers) if providers else None
        self.input_size = input_size
        self.backend = 'scrfd'
        self.active_provider = ''
        self.load_notes = []
        self.last_latency_ms = 0.0
        self._det = None

    def load(self):
        if self._det is None:
            self._det = ScrfdDetector(self.model_path, input_size=self.input_size,
                                      conf=self.conf, providers=self.providers)
            active = self._det.session.get_providers()
            self.active_provider = active[0] if active else 'CPUExecutionProvider'
        return self

    @property
    def description(self) -> str:
        return 'SCRFD-500m / onnxruntime'

    @property
    def device_label(self) -> str:
        return 'GPU CUDA' if 'CUDA' in (self.active_provider or '') else 'CPU'

    def detect(self, frame):
        import time
        from vision.face_detector import FaceBox

        if self._det is None:
            self.load()
        started = time.perf_counter()
        raw = self._det.detect(frame)
        self.last_latency_ms = (time.perf_counter() - started) * 1000.0
        faces = []
        for item in raw:
            if item['score'] < self.conf:
                continue
            x1, y1, x2, y2 = [int(round(v)) for v in item['box']]
            kps = item['kps']
            # FaceBox 的约定是每行 (置信, x, y)；SCRFD 不给点置信，按 1.0 记
            keypoints = np.stack([np.ones(len(kps), np.float32),
                                  kps[:, 0].astype(np.float32),
                                  kps[:, 1].astype(np.float32)], axis=1)
            faces.append(FaceBox(x1=x1, y1=y1, x2=x2, y2=y2,
                                 score=float(item['score']), keypoints=keypoints))
        return faces
