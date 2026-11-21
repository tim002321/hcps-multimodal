# -*- coding: utf-8 -*-
"""运行参数：默认值与主程序对齐，fingerprint 只收"影响像素结果"的字段。

为什么要 fingerprint：续跑时若参数变了（比如眼 ROI 比例从 0.32 改成 0.40），
旧产物和新产物混在一个目录里，下游无法分辨。params_hash 一变就全量重算。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields


def default_models_dir() -> str:
    """模型目录：exe 同级 models 优先（可替换），其次包内 models，最后主程序目录。

    优先级这么排是因为打包分发后，用户想换权重时直接丢到 exe 旁边的 models/
    就应该生效，不需要重新打包。
    """
    from .paths import app_root, bundle_root
    candidates = [
        os.path.join(app_root(), 'models'),
        os.path.join(bundle_root(), 'models'),
        os.path.join(os.path.dirname(app_root()), 'AAAwindows_code', 'models'),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0]


@dataclass(frozen=True)
class RunParams:
    """一次处理运行的全部可调项。"""

    # ---- 检测 ----
    backend: str = 'auto'          # auto | torch | onnx | dlib
    conf: float = 0.35             # 与主程序一致
    imgsz: int = 640
    device: str = ''               # '' = 自动
    face_pick: str = 'center'      # center | largest
    center_band: float = 0.25      # 与主程序 pick_center_face 一致
    min_face_px: int = 64

    # ---- ROI ----
    # 边距给得比"刚好框住五官"宽一档：ROI 裁出去就扩不回来，宁可文件大点也别留遗憾。
    roi_mode: str = 'hybrid'       # hybrid | face_ratio | landmark_box
    eye_w_ratio: float = 0.40
    eye_h_ratio: float = 0.26
    eye_box_w: float = 2.2         # 眼周包围盒宽倍数
    eye_box_h: float = 2.8
    mouth_w_scale: float = 1.80    # 外唇宽倍数
    mouth_h_scale: float = 3.60    # 外唇高倍数
    mouth_dy: float = 0.12         # 嘴部中心下移比例（×高）
    roi_scale: float = 1.0         # 1 / 2 / 3 / 4
    jpeg_quality: int = 95
    eye_format: str = 'png'        # 眼部存 png（无损）还是 jpg

    # ---- 产物 ----
    # 标注图是"在原图上画线"的产物，本质仍是人脸图像，默认不生成。
    # 自检需要时再打开，且不要外传。
    annotate: bool = False
    annotate_index: bool = True

    # ---- 运行控制（不进 fingerprint）----
    overwrite: bool = False
    limit: int = 0
    models_dir: str = ''
    include_orphans: bool = True

    # ------------------------------------------------------------------
    def resolved_models_dir(self) -> str:
        return self.models_dir or default_models_dir()

    def to_dict(self) -> dict:
        data = asdict(self)
        data['models_dir'] = self.resolved_models_dir()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'RunParams':
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**clean)

    # ------------------------------------------------------------------
    def fingerprint(self) -> dict:
        """真正影响写盘像素的字段。开关类（overwrite/limit/annotate/device）排除。"""
        keys = (
            'backend', 'conf', 'imgsz', 'face_pick', 'center_band', 'min_face_px',
            'roi_mode', 'eye_w_ratio', 'eye_h_ratio', 'eye_box_w', 'eye_box_h',
            'mouth_w_scale', 'mouth_h_scale', 'mouth_dy', 'roi_scale',
            'jpeg_quality', 'eye_format', 'annotate_index',
        )
        return {k: getattr(self, k) for k in keys}

    def params_hash(self) -> str:
        blob = json.dumps(self.fingerprint(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]

    def problems(self) -> list:
        """启动前自检，返回中文问题列表（空表示没问题）。"""
        out = []
        if self.backend not in ('auto', 'torch', 'onnx', 'dlib'):
            out.append('检测后端只能是 auto/torch/onnx/dlib')
        if not 0.05 <= self.conf <= 0.95:
            out.append('置信度应在 0.05~0.95')
        if self.roi_mode not in ('hybrid', 'face_ratio', 'landmark_box'):
            out.append('ROI 模式只能是 hybrid/face_ratio/landmark_box')
        if not 0.10 <= self.eye_w_ratio <= 0.60:
            out.append('眼宽比应在 0.10~0.60')
        if not 0.06 <= self.eye_h_ratio <= 0.40:
            out.append('眼高比应在 0.06~0.40')
        if not 1.0 <= self.mouth_w_scale <= 3.0:
            out.append('嘴宽倍数应在 1.0~3.0')
        if not 1.0 <= self.mouth_h_scale <= 6.0:
            out.append('嘴高倍数应在 1.0~6.0')
        if self.roi_scale not in (1, 2, 3, 4):
            out.append('放大倍数只能是 1/2/3/4')
        if not 60 <= self.jpeg_quality <= 100:
            out.append('JPEG 质量应在 60~100')
        if self.eye_format not in ('png', 'jpg'):
            out.append('眼部格式只能是 png 或 jpg')
        return out
