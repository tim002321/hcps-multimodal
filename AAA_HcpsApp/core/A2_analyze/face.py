# -*- coding: utf-8 -*-
"""表情识别（A2）：视频帧 -> 每帧一个表情标签。

来源：老工程 face_model 的 EOMTION_FOR_YOU.py + facial_tools（CNN3 网络 + BlazeFace 检测）。
判定口径逐字照搬：

1. 人脸检测：BlazeFace（tflite），框从 xyxy 转 xywh 且宽高 ×1.1
2. 每张脸取 ``gray[y:y+h+10, x:x+w+10]`` 后做 6 种增广（原图 / 上移 2 行 / 水平翻转 /
   三个 45×45 角），统一缩到 48×48 灰度图
3. 6 张一起喂 CNN3，**把 6 个 softmax 相加**（老代码 `np.sum(results, axis=0)`）再取 argmax
4. 8 类标签，顺序同老 utils.index2emotion：
   anger / disgust / fear / happy / sad / surprised / neutral / contempt
5. 一张脸都没检出来 -> 表情记 'no_face'，概率全 0（老代码就是这样）

相对老代码的改动（只改壳 / 修硬编码）：

* 权重路径走配置（老代码里写死成 `C:\\Users\\Admin\\...\\cnn3_best_weights.h5`）
* 检测器只用 **正脸模型**（front，128×128）。老代码 `blaze_detect(img)` 默认吃的是
  256×256 的 back 模型，那个侧向/后置模型按项目要求先没搬，需要时把 tflite 拷回
  `face_tools/blazeface/weights/` 并把 `detector_model` 配成 'back' 即可。
* 老代码的 'default' 分支里把 haarcascade 路径写死在别人的 conda 目录里，新版改用
  cv2 自带的 `cv2.data.haarcascades`（detector 配成 'haar' 时生效）
* 老 `processget_facial_expression` 是半成品（只处理单图，算完就 print），
  这里补上会话级遍历：按 data_video_frame 表逐帧识别，返回 DataFrame
* 检测器实例缓存复用（老代码每帧都重建 tflite 解释器），结果不变
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.A2_analyze.load_model import resolve_models_dir

log = logging.getLogger(__name__)

# 类别顺序 = 老工程 facial_tools/utils.py index2emotion 的字典顺序，别改
EMOTIONS: List[str] = ['anger', 'disgust', 'fear', 'happy',
                       'sad', 'surprised', 'neutral', 'contempt']
EMOTIONS_CN: List[str] = ['发怒', '厌恶', '恐惧', '开心', '伤心', '惊讶', '中性', '蔑视']
N_CLASSES = len(EMOTIONS)
NO_FACE = 'no_face'
READ_ERROR = 'read_error'

DEFAULT_CONFIG: Dict[str, Any] = {
    'model_weights': 'cnn3_best_weights.h5',    # 相对 models_dir
    'detector': 'blazeface',                    # blazeface / haar
    'detector_model': 'front',                  # 只搬了正脸模型
    'score_threshold': 0.7,
    'iou_threshold': 0.3,
    'img_size': 48,
    'augment': True,                            # 老代码固定做 6 张增广
    'box_pad': 10,                              # 老代码 x:x+w+10 / y:y+h+10
    'step': 1,                                  # 每 step 帧识别一帧
}

DB_TABLE = 'data_video_frame'
DB_TIME_COLUMN = 'created_date'


def index2emotion(index: int, kind: str = 'en') -> str:
    """下标 -> 表情名（老工程 utils.index2emotion，kind='cn' 返回中文）。"""
    index = int(index)
    return EMOTIONS_CN[index] if kind == 'cn' else EMOTIONS[index]


class FaceExpressionAnalyzer:
    """人脸检测 + 表情分类。模型懒加载，构造对象本身不碰 tensorflow。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.models_dir = resolve_models_dir(cfg.get('models_dir'))
        self.model_weights = str(cfg['model_weights'])
        self.detector = str(cfg.get('detector', 'blazeface')).lower()
        self.detector_model = str(cfg.get('detector_model', 'front')).lower()
        self.score_threshold = float(cfg.get('score_threshold', 0.7))
        self.iou_threshold = float(cfg.get('iou_threshold', 0.3))
        self.img_size = int(cfg.get('img_size', 48))
        self.augment = bool(cfg.get('augment', True))
        self.box_pad = int(cfg.get('box_pad', 10))
        self.step = max(1, int(cfg.get('step', 1)))
        self._model = None
        self._blaze = None
        self._haar = None

    # ------------------------------------------------------------- 模型
    @property
    def weights_path(self) -> str:
        if os.path.isabs(self.model_weights):
            return self.model_weights
        return os.path.join(self.models_dir, self.model_weights)

    def load(self) -> 'FaceExpressionAnalyzer':
        """构建 CNN3 并加载权重（幂等）。"""
        if self._model is not None:
            return self
        path = self.weights_path
        if not os.path.exists(path):
            raise FileNotFoundError(f'表情模型权重不存在：{path}')

        from core.A2_analyze.face_tools.model import CNN3     # 内部会 import tensorflow

        model = CNN3()
        model.load_weights(path)
        self._model = model
        log.info('[face] CNN3 权重已加载：%s（%d 个参数）', path, model.count_params())
        return self

    # ------------------------------------------------------------- 人脸检测
    def detect_faces(self, image_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
        """返回 [(x, y, w, h), ...]（老代码的 xyxy -> xywh，宽高 ×1.1）。"""
        if self.detector == 'haar':
            return self._detect_haar(image_bgr)
        return self._detect_blazeface(image_bgr)

    def _detect_blazeface(self, image_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
        from core.A2_analyze.face_tools.blazeface import blazeFaceDetector, xyxy_to_xywh

        if self._blaze is None:
            # 检测器建一次就复用（老代码每帧重建 tflite 解释器，慢）
            self._blaze = blazeFaceDetector(self.detector_model, self.score_threshold, self.iou_threshold)
        height, width, _ = image_bgr.shape
        results = self._blaze.detectFaces(image_bgr)

        boxes = []
        for bounding_box in results.boxes:
            x1 = (width * bounding_box[0]).astype(int)
            x2 = (width * bounding_box[2]).astype(int)
            y1 = (height * bounding_box[1]).astype(int)
            y2 = (height * bounding_box[3]).astype(int)
            boxes.append([x1, y1, x2, y2])
        if not boxes:
            return []

        boxes_array = xyxy_to_xywh(np.array(boxes).astype('int'))
        boxes_array[:, 2] = boxes_array[:, 2] * 1.1
        boxes_array[:, 3] = boxes_array[:, 3] * 1.1
        return [tuple(int(v) for v in box) for box in boxes_array]

    def _detect_haar(self, image_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
        import cv2

        if self._haar is None:
            cascade_path = os.path.join(cv2.data.haarcascades, 'haarcascade_frontalface_alt.xml')
            self._haar = cv2.CascadeClassifier(cascade_path)
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        faces = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=1, minSize=(30, 30))
        return [tuple(int(v) for v in box) for box in faces]

    # ------------------------------------------------------------- 表情分类
    def augment_faces(self, face_gray: np.ndarray) -> np.ndarray:
        """老代码 generate_faces：6 张增广，返回 (n, 48, 48, 1)。"""
        import cv2

        face = face_gray / 255.
        face = cv2.resize(face, (self.img_size, self.img_size), interpolation=cv2.INTER_LINEAR)
        crops = [face[:, :], face[2:45, :], cv2.flip(face[:, :], 1),
                 face[0:45, 0:45], face[2:47, 0:45], face[2:47, 2:47]]
        images = []
        for crop in crops:
            resized = cv2.resize(crop, (self.img_size, self.img_size))
            images.append(np.expand_dims(resized, axis=-1))
        return np.array(images)

    def predict_face(self, face_gray: np.ndarray) -> Tuple[str, np.ndarray, np.ndarray]:
        """一张灰度人脸 -> (表情名, 6 张增广的 softmax 之和, 单张增广的 softmax)。"""
        self.load()
        batch = self.augment_faces(face_gray)
        results = self._model.predict(batch, verbose=0)
        summed = np.sum(results, axis=0).reshape(-1)
        return index2emotion(int(np.argmax(summed))), summed, results

    def predict_image(self, image_bgr: np.ndarray) -> Dict[str, Any]:
        """一张图 -> {'emotions', 'probabilities', 'boxes'}（多张脸就是多个元素）。"""
        import cv2

        faces = self.detect_faces(image_bgr)
        if not faces:
            return {'emotions': [NO_FACE], 'probabilities': [np.zeros(N_CLASSES)], 'boxes': []}

        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        emotions: List[str] = []
        probabilities: List[np.ndarray] = []
        for (x, y, w, h) in faces:
            face_gray = gray[y:y + h + self.box_pad, x:x + w + self.box_pad]
            try:
                emotion, summed, _ = self.predict_face(face_gray)
            except Exception as exc:                     # noqa: BLE001 - 框太小之类，别拖垮整段
                log.warning('[face] 表情识别失败（框 %s）：%s', (x, y, w, h), exc)
                continue
            emotions.append(emotion)
            probabilities.append(summed)
        if not emotions:
            return {'emotions': [NO_FACE], 'probabilities': [np.zeros(N_CLASSES)], 'boxes': faces}
        return {'emotions': emotions, 'probabilities': probabilities, 'boxes': faces}

    def predict_path(self, image_path: str) -> Dict[str, Any]:
        """按文件路径识别（老代码 predict_expression 的入口）。"""
        image = self.read_image(image_path)
        if image is None:
            return {'emotions': [READ_ERROR], 'probabilities': [np.zeros(N_CLASSES)], 'boxes': []}
        return self.predict_image(image)

    @staticmethod
    def decode_frame(jpeg_bytes: bytes) -> Optional[np.ndarray]:
        """把一帧 JPEG 字节解成 BGR 图（摄像头实时监测用，不落盘）。"""
        import cv2

        if not jpeg_bytes:
            return None
        buffer = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)

    @staticmethod
    def read_image(image_path: str) -> Optional[np.ndarray]:
        """读图。会话目录带中文（实验结果/…），Windows 下 cv2.imread 会直接返回 None——
        老工程为此专门写了 read.ImageReader 把图拷到临时目录再读；这里用 imdecode 绕过，等价且干净。
        """
        import cv2

        try:
            buffer = np.fromfile(str(image_path), dtype=np.uint8)
        except OSError as exc:
            log.warning('[face] 读图失败（%s）：%s', image_path, exc)
            return None
        if buffer.size == 0:
            log.warning('[face] 图片是空文件：%s', image_path)
            return None
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)

    # ------------------------------------------------------------- 会话级
    @staticmethod
    def read_frames(db_path: str, start: Any = None,
                    end: Any = None) -> List[tuple]:
        """读 data_video_frame：(camera, frame_index, path, created_date)。"""
        conn = sqlite3.connect(str(db_path))
        try:
            if start is None or end is None:
                first, last = conn.execute(
                    f'SELECT MIN({DB_TIME_COLUMN}), MAX({DB_TIME_COLUMN}) FROM {DB_TABLE}').fetchone()
                if first is None:
                    raise RuntimeError(f'{db_path} 的 {DB_TABLE} 表里没有数据')
                start = start if start is not None else first
                end = end if end is not None else last
            cursor = conn.execute(
                f'SELECT camera, frame_index, path, {DB_TIME_COLUMN} FROM {DB_TABLE}'
                f' WHERE {DB_TIME_COLUMN} BETWEEN ? AND ?', (str(start), str(end)))
            return cursor.fetchall()
        finally:
            conn.close()

    def analyze(self, db_path: str, start: Any = None, end: Any = None) -> pd.DataFrame:
        """会话库 -> 逐帧表情表（time, camera, frame_index, path, emotion）。"""
        rows = self.read_frames(db_path, start, end)
        if not rows:
            raise RuntimeError(f'{db_path} 在 {start} ~ {end} 之间没有视频帧记录')

        records = []
        for index, (camera, frame_index, path, created) in enumerate(rows):
            if index % self.step:
                continue
            result = self.predict_path(path)
            records.append({
                'time': created,
                'camera': camera,
                'frame_index': int(frame_index),
                'path': path,
                'emotion': result['emotions'][0],
                'faces': len(result['boxes']),
            })
        log.info('[face] 识别 %d 帧（共 %d 条记录，step=%d）', len(records), len(rows), self.step)
        return pd.DataFrame(records)
