# -*- coding: utf-8 -*-
"""张嘴判定：HippoYD 的 ONNX 分类器。

模型来历：https://github.com/iglaweb/HippoYD（Apache-2.0），用 YawDD 打哈欠数据集训练，
官方报告 test split 准确率 0.988 / F1 0.967。权重 `models/yawn_model_80.onnx`（9.4 MB）。

输入输出规格是照它的推理脚本 `src/run_yawn_inference_onnx_onnxruntime.py`
和 `src/model_config.py` 核过的，**不要随意改预处理**，改了分布就变了：

    输入   (N, 100, 100, 1) float32
    预处理  人脸 ROI（整张脸，不是嘴部裁剪）→ 灰度 → resize 100×100 → /255.0
    输出   (N, 1) float32，值即「张嘴概率」
    训练口径 MAR(mouth aspect ratio) >= 0.6 记为张嘴

注意一个容易踩的点：它吃的是**整张人脸**，不是嘴巴小块。
所以上游做人脸检测时框要尽量贴合脸（YOLOv8n-face 的输出正好合适）。
"""
from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np

# 与 model_config.py 保持一致
INPUT_SIZE = (100, 100)
COLOR_CHANNELS = 1

# 判定阈值。上游脚本用的是 0.2（很松，适合车载场景），
# 我们的被试距离/光照/镜头都不同，所以这里给个中性的 0.5 作初值，
# 真实阈值必须用 tools/vision/calibrate_threshold.py 在本地素材上标定。
DEFAULT_THRESHOLD = 0.5

DEFAULT_MODEL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'models', 'yawn_model_80.onnx')


class MouthClassifier:
    """人脸 ROI → 张嘴概率。

    用法：
        clf = MouthClassifier('models/yawn_model_80.onnx')
        prob = clf.predict(face_bgr)          # 0.0 ~ 1.0
        if clf.is_open(prob): ...

    推理后端（`backend` 参数）：
        auto        默认。优先 cv2.dnn，失败再退 onnxruntime
        cv2         只用 cv2.dnn
        onnxruntime 只用 onnxruntime

    为什么默认 cv2.dnn：两者在真实人脸框上输出**逐位相同**（实测 15 帧最大差值 0），
    cv2.dnn 单帧 2.35ms，且不需要加载 onnxruntime 那套原生库。onnxruntime 与 torch
    各带一份 OpenMP/protobuf，在 Windows + PyQt5 里后加载会直接段错误
    （详见 vision/yawn_monitor.py 里 `_preload_heavy_modules` 的说明），能不用就不用。
    """

    def __init__(self, model_path: Optional[str] = None, threshold: float = DEFAULT_THRESHOLD,
                 providers: Optional[list] = None, backend: str = 'auto'):
        self.model_path = model_path or DEFAULT_MODEL
        self.threshold = float(threshold)
        self.backend = backend
        self.engine = ''          # 实际生效的后端：'cv2' / 'onnxruntime'
        self._session = None      # onnxruntime 会话
        self._net = None          # cv2.dnn 网络
        self._input_name = ''
        self._output_name = ''
        self.last_latency_ms = 0.0
        self._providers = providers

    # ------------------------------------------------------------------
    def load(self) -> 'MouthClassifier':
        """加载模型（幂等）。缺文件时抛 RuntimeError，附解决提示。"""
        if self._session is not None or self._net is not None:
            return self
        if not os.path.exists(self.model_path):
            raise RuntimeError(
                '缺少张嘴模型：{}\n请先运行 python tools/vision/fetch_models.py'
                .format(self.model_path))

        want = (self.backend or 'auto').lower()
        errors = []

        if want in ('auto', 'cv2', 'opencv'):
            try:
                import cv2
                self._net = cv2.dnn.readNetFromONNX(self.model_path)
                self.engine = 'cv2'
                return self
            except Exception as exc:                      # noqa: BLE001 - 退到下一个后端
                errors.append('cv2.dnn 不可用：{}'.format(exc))

        if want in ('auto', 'onnxruntime', 'onnx'):
            try:
                import onnxruntime as ort
                options = ort.SessionOptions()
                options.log_severity_level = 3            # 屏蔽 Keras 导出时的 initializer 告警
                self._session = ort.InferenceSession(
                    self.model_path, sess_options=options,
                    providers=self._providers or ['CPUExecutionProvider'])
                self._input_name = self._session.get_inputs()[0].name
                self._output_name = self._session.get_outputs()[0].name
                self.engine = 'onnxruntime'
                return self
            except ImportError:
                errors.append('缺少 onnxruntime（pip install onnxruntime）')
            except Exception as exc:                      # noqa: BLE001
                errors.append('onnxruntime 加载失败：{}'.format(exc))

        raise RuntimeError('张嘴模型加载失败：\n  ' + '\n  '.join(errors))

    # ------------------------------------------------------------------
    @staticmethod
    def preprocess(face_bgr: np.ndarray) -> np.ndarray:
        """人脸 ROI → (1,100,100,1) float32。逐行对齐上游脚本，别改。"""
        import cv2

        if face_bgr is None or face_bgr.size == 0:
            raise ValueError('空的人脸图像')
        gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY) \
            if face_bgr.ndim == 3 else face_bgr
        if gray.shape[0] != INPUT_SIZE[1] or gray.shape[1] != INPUT_SIZE[0]:
            gray = cv2.resize(gray, INPUT_SIZE)
        frame = gray[:, :, np.newaxis].astype(np.float32) / 255.0
        return np.expand_dims(frame, 0)

    def predict(self, face_bgr: np.ndarray) -> float:
        """返回张嘴概率（0~1）。模型没加载会自动加载。"""
        if self._session is None and self._net is None:
            self.load()
        blob = self.preprocess(face_bgr)
        started = time.perf_counter()
        if self._net is not None:
            self._net.setInput(blob)
            output = self._net.forward()
            prob = float(np.asarray(output).reshape(-1)[0])
        else:
            output = self._session.run([self._output_name], {self._input_name: blob})[0]
            prob = float(np.asarray(output).reshape(-1)[0])
        self.last_latency_ms = (time.perf_counter() - started) * 1000.0
        return prob

    def is_open(self, prob: float) -> bool:
        return prob >= self.threshold
