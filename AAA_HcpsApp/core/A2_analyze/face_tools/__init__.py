# -*- coding: utf-8 -*-
"""表情识别的工具件（从老工程 face_model/facial_tools 原样搬来）。

* model.py      CNN3 网络结构（老工程 facial_tools/model.py，一字未改）
* blazeface/    BlazeFace tflite 人脸检测（老工程 facial_tools/blazeface 整包搬来）

这两个都会 import tensorflow，所以 A2 的其它模块不要从包级再往外导它们；
需要时用 `from core.A2_analyze.face_tools.blazeface import blaze_detect` 单点导入。
"""
