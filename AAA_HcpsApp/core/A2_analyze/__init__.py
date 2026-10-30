# -*- coding: utf-8 -*-
"""A2 分析层：数据库/DataFrame -> 特征 / 结果。全部是纯算法，不碰设备也不碰界面。

* grip.py        握力加工：data_grip_raw -> 老格式 data_grip_l/r
* ecg.py         心电特征：data_ecg -> HRV 特征表（小波去噪 -> 滑窗 -> R 峰 -> RR -> 特征）
* eye.py         眼动窗口特征：data_udp -> pupil / blink_rate
* load_model.py  负荷等级：10 维特征 -> SVM / CatBoost / XGBoost
* pipeline.py    上面三步串起来：build_features / assess / analyze
* face.py        表情识别：视频帧 -> 表情标签（CNN3 + BlazeFace，tensorflow 懒加载）
* face_tools/    CNN3 网络结构 + BlazeFace 检测（整包搬自老工程 facial_tools）

注意：本包 __init__ 会连带导入 numpy/pandas/pywt/biosppy/hrvanalysis 这些分析依赖，
只想握力加工的话可以 `from core.A2_analyze.grip import process_raw_table` 单点导入。
tensorflow/cv2 只在 face.py 真正用到时才导入，不会拖慢其它模块。
"""
from core.A2_analyze.ecg import EcgProcessor, compute_ecg_features          # noqa: F401
from core.A2_analyze.eye import EyeProcessor, compute_eye_features          # noqa: F401
from core.A2_analyze.face import (EMOTIONS, EMOTIONS_CN, NO_FACE,  # noqa: F401
                                  FaceExpressionAnalyzer, index2emotion)
from core.A2_analyze.grip import GripProcessor, Kalman1D, process_raw_table  # noqa: F401
from core.A2_analyze.load_model import (FEATURE_COLUMNS, LoadAssessment,  # noqa: F401
                                        load_assessment)
from core.A2_analyze.pipeline import analyze, assess, build_features        # noqa: F401

__all__ = [
    'GripProcessor', 'Kalman1D', 'process_raw_table',
    'EcgProcessor', 'compute_ecg_features',
    'EyeProcessor', 'compute_eye_features',
    'LoadAssessment', 'load_assessment', 'FEATURE_COLUMNS',
    'FaceExpressionAnalyzer', 'EMOTIONS', 'EMOTIONS_CN', 'NO_FACE', 'index2emotion',
    'build_features', 'assess', 'analyze',
]
