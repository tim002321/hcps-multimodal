# -*- coding: utf-8 -*-
"""视觉推理层：摄像头画面 → 人脸 → 张嘴概率 → 疲劳状态。

本包只做「从图像算出结论」，不碰界面、不碰数据库、不碰采集线程：

| 模块 | 职责 | 依赖 |
|---|---|---|
| `face_detector.py` | YOLOv8n-face 检测人脸框（后端可切 torch / onnxruntime） | ultralytics+torch 或 onnxruntime |
| `mouth_state.py` | HippoYD 判「张嘴」概率 | onnxruntime + opencv |
| `yawn_logic.py` | 时序状态机：把逐帧概率变成「哈欠事件」与疲劳等级 | **无**（纯 Python，可单测） |
| `yawn_monitor.py` | 后台线程：定时取帧 → 推理 → 更新状态快照 | 上面三个 |

设计约束（与 `core/writer_thread.py`、`core/device_status.py` 保持一致）：
  * 重活都在后台线程，GUI 只读加锁快照；
  * 推理线程不 import 任何 PyQt 对象；
  * 模型缺失/依赖缺失要给出可读提示，不能抛到界面上变成白屏。

模型权重不进版本库，用 `tools/vision/fetch_models.py` 获取。
"""
from __future__ import annotations

__all__ = ['FaceDetector', 'MouthClassifier', 'YawnLogic', 'YawnMonitor', 'YawnState']


def _lazy():
    """延迟导入：让只用到状态机的场合（如单测）不必装 torch/onnxruntime。"""
    from vision.face_detector import FaceDetector
    from vision.mouth_state import MouthClassifier
    from vision.yawn_logic import YawnLogic
    from vision.yawn_monitor import YawnMonitor, YawnState
    return FaceDetector, MouthClassifier, YawnLogic, YawnMonitor, YawnState


def __getattr__(name):                                # PEP 562
    if name in __all__:
        return dict(zip(__all__, _lazy()))[name]
    raise AttributeError(name)
