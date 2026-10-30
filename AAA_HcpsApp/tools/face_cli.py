# -*- coding: utf-8 -*-
"""表情识别的命令行入口：把 TensorFlow 关在独立进程里。

为什么单独跑：TensorFlow 和 PyQt5 在同一个 Windows 进程里会偶发访问违例
（0xc0000005，弹"内存不能为 read"那种框）。界面进程只要不 import tf 就彻底躲开这类崩溃，
识别慢一点、多一个进程，比整个界面挂掉划算。

用法：
    python tools/face_cli.py 图片.jpg                # 单次识别，stdout 一行 JSON
    python tools/face_cli.py --serve                 # 常驻：stdin 一行一个路径，stdout 一行一个 JSON
    python tools/face_cli.py --serve                 # 常驻也吃整帧：{"id": 12, "b64": "<JPEG base64>"}
    python tools/face_cli.py 图片.jpg --config config/default.yaml
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A2_analyze import FaceExpressionAnalyzer        # noqa: E402


def load_face_config(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as handle:
        return ((yaml.safe_load(handle) or {}).get('analyze') or {}).get('face') or {}


def predict(analyzer: FaceExpressionAnalyzer, image_path: str) -> Dict[str, Any]:
    """识别一张图；出错也返回可 JSON 化的字典，让界面自己决定怎么显示。"""
    try:
        result = analyzer.predict_path(image_path)
    except Exception as exc:                          # noqa: BLE001
        return {'path': image_path, 'error': f'{type(exc).__name__}: {exc}'}
    return _payload(image_path, result)


def predict_frame(analyzer: FaceExpressionAnalyzer, jpeg_bytes: bytes, tag: str = 'frame'
                  ) -> Dict[str, Any]:
    """识别一帧（摄像头来的 JPEG 字节，界面实时监测走这条）。"""
    import cv2
    import numpy as np

    buffer = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return {'path': tag, 'error': '帧解码失败'}
    try:
        result = analyzer.predict_image(image)
    except Exception as exc:                          # noqa: BLE001
        return {'path': tag, 'error': f'{type(exc).__name__}: {exc}'}
    return _payload(tag, result)


def _payload(tag: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'path': tag,
        'emotions': list(result['emotions']),
        'boxes': [[int(v) for v in box] for box in result['boxes']],
        'probabilities': [[float(v) for v in values] for values in result['probabilities']],
    }


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='表情识别（独立进程）')
    parser.add_argument('image', nargs='?', help='图片路径')
    parser.add_argument('--serve', action='store_true', help='常驻模式：从 stdin 收路径')
    parser.add_argument('--config', default=str(ROOT / 'config' / 'default.yaml'))
    args = parser.parse_args()

    config = load_face_config(args.config)
    analyzer = FaceExpressionAnalyzer(config)
    if not args.serve and not args.image:
        parser.error('要么给图片路径，要么用 --serve')
    if not args.serve:
        emit(predict(analyzer, args.image))
        return 0

    analyzer.load()                                   # 常驻模式：先把模型热起来
    emit({'ready': True})
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text in ('__quit__', '__exit__'):
            break
        if text.startswith('{'):                      # 整帧：{"id": n, "b64": "..."}
            try:
                request = json.loads(text)
            except json.JSONDecodeError:
                emit({'error': '请求不是合法 JSON'})
                continue
            frame_id = request.get('id')
            try:
                jpeg = base64.b64decode(request.get('b64') or '')
            except Exception as exc:                  # noqa: BLE001
                emit({'id': frame_id, 'error': f'base64 解码失败：{exc}'})
                continue
            payload = predict_frame(analyzer, jpeg, f'frame:{frame_id}')
            payload['id'] = frame_id
            emit(payload)
        else:
            emit(predict(analyzer, text))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
