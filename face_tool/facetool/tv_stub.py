# -*- coding: utf-8 -*-
"""打包环境下的 torchvision 占位。

背景：torchvision.ops.roi_align → torch._dynamo.utils.is_compile_supported
→ torch._numpy，这条链在 PyInstaller 冻结晶后有已知缺陷，会抛
"name 'name' is not defined"。而 ultralytics 在
`utils/nms.py:99` 用 `"torchvision" in sys.modules` 来判断是否走
torchvision 的 NMS，所以只要 import 成功，它就会真的调 `ops.nms`。

因此这里提供一个**可用**的最小实现：只给 ops.nms（纯 torch，语义与
torchvision 一致），其余属性留给真正的 torchvision 或直接不提供。

只在冻结（打包）环境安装；源码运行时不动 sys.modules。
"""
from __future__ import annotations

import sys
import types


def _build_nms():
    import torch

    def nms(boxes, scores, iou_threshold):
        if boxes.numel() == 0:
            return torch.empty((0,), dtype=torch.long, device=boxes.device)
        x1, y1, x2, y2 = boxes.unbind(1)
        areas = (x2 - x1).clamp(min=0) * (y2 - y1).clamp(min=0)
        order = scores.argsort(descending=True)
        keep = []
        while order.numel() > 0:
            i = int(order[0])
            keep.append(i)
            if order.numel() == 1:
                break
            rest = order[1:]
            xx1 = torch.clamp(x1[rest], min=float(x1[i]))
            yy1 = torch.clamp(y1[rest], min=float(y1[i]))
            xx2 = torch.clamp(x2[rest], max=float(x2[i]))
            yy2 = torch.clamp(y2[rest], max=float(y2[i]))
            inter = (xx2 - xx1).clamp(min=0) * (yy2 - yy1).clamp(min=0)
            iou = inter / (areas[i] + areas[rest] - inter).clamp(min=1e-9)
            order = rest[iou <= float(iou_threshold)]
        return torch.tensor(keep, dtype=torch.long, device=boxes.device)

    return nms


def install() -> bool:
    """安装占位模块，返回是否安装（冻结环境且 torchvision 尚未加载时为 True）。"""
    if not getattr(sys, 'frozen', False):
        return False
    if 'torchvision' in sys.modules:
        return False
    try:
        nms = _build_nms()
    except Exception:                                  # noqa: BLE001
        return False

    tv = types.ModuleType('torchvision')
    tv.__version__ = '0.0.0+stub'
    tv.__path__ = []
    ops = types.ModuleType('torchvision.ops')
    ops.__path__ = []
    ops.nms = nms
    tv.ops = ops
    sys.modules['torchvision'] = tv
    sys.modules['torchvision.ops'] = ops
    return True
