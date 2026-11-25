# -*- coding: utf-8 -*-
"""枚举本机摄像头：**设备名 + OpenCV 索引**。

为什么需要这一层
----------------
config.ini 里的 ``[camera] index`` 是 DirectShow 的枚举序号。插拔一次 USB、
换个口、接上笔记本自带摄像头，序号就可能变；而界面上只显示「设备号 0/1」，
用户分不清哪个才是对着自己的那台。所以这里把「名字」也取出来。

两个来源，按可靠性排序
----------------------
1. **ffmpeg -list_devices**（首选）
   能拿到真名字，顺序就是 DirectShow 的枚举顺序，与 ``cv2.CAP_DSHOW`` 的
   index 一一对应；只在枚举瞬间打开一次 DShow，**不会独占总线很久**，
   采集进行中也能列。
2. **逐个 index 探测**（兜底）
   没装 ffmpeg 时用：逐个 ``cv2.VideoCapture(i, CAP_DSHOW)`` 试着打开，
   能开就算一个。名字只能写「摄像头 N」，而且探测期间会短暂占用设备
   （正在采集时探测会失败，所以采集状态下不要刷新）。

偏好持久化
----------
**不写 config.ini**：configparser 回写会把里面整段中文注释全丢掉（见
ui/profile.py 同样的取舍）。选择结果单独存 ``data/camera_prefs.json``，
只有 index 和名字两个字段，坏了删掉即可。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional

from core import paths

# 枚举结果缓存：ffmpeg 列一遍约 0.3~1 秒，翻页/切页时不想每次都跑
_CACHE_TTL = 8.0
_cache: Dict[str, object] = {'at': 0.0, 'items': []}

PREFS_PATH = paths.app_path('data', 'camera_prefs.json')

# ffmpeg 输出里视频设备是这一行：  [dshow @ 0000] "4K AutoFocus Webcam" (video)
_VIDEO_RE = re.compile(r'\[dshow[^\]]*\]\s+"([^"]+)"\s+\(video\)')
_ANY_RE = re.compile(r'\[(?:dshow|in#0)[^\]]*\]\s+"([^"]+)"')


def find_ffmpeg() -> str:
    """找一个能用的 ffmpeg.exe：环境变量 > PATH > 常见位置。找不到返回 ''。"""
    env = os.environ.get('HCP_FFMPEG') or os.environ.get('EYE_FFMPEG')
    if env and os.path.isfile(env):
        return env
    hit = shutil.which('ffmpeg')
    if hit:
        return hit
    for cand in (r'C:\ffmpeg\bin\ffmpeg.exe',
                 r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
                 r'C:\Users\lenovo\anaconda3\Library\bin\ffmpeg.exe'):
        if os.path.isfile(cand):
            return cand
    return ''


def _names_from_ffmpeg(ffmpeg: str, timeout: float = 8.0) -> List[str]:
    """用 ffmpeg 列 DirectShow 视频设备名；失败返回空列表。"""
    if not ffmpeg:
        return []
    try:
        out = subprocess.run([ffmpeg, '-hide_banner', '-list_devices', 'true',
                              '-f', 'dshow', '-i', 'dummy'],
                             capture_output=True, text=True, timeout=timeout,
                             encoding='utf-8', errors='replace').stderr or ''
    except Exception:                                     # noqa: BLE001
        return []
    names = _VIDEO_RE.findall(out)
    if not names:
        # 老版本 ffmpeg 不带 (video) 标记，退一步：去掉明显是音频的设备名
        names = [n for n in _ANY_RE.findall(out)
                 if '麦克风' not in n and 'microphone' not in n.lower()
                 and 'line in' not in n.lower() and '立体声' not in n]
    # 去重保序（有些驱动会把自己的虚拟设备列两次）
    seen, uniq = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    return uniq


def _probe_indices(max_index: int) -> List[int]:
    """没 ffmpeg 时兜底：逐个 index 试着打开，能开就算一个。"""
    try:
        import cv2
    except Exception:                                     # noqa: BLE001
        return []
    found = []
    for index in range(max(0, max_index)):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        try:
            if cap.isOpened():
                found.append(index)
        finally:
            cap.release()
    return found


def list_cameras(max_index: int = 8, probe: bool = True,
                 use_cache: bool = True) -> List[Dict[str, object]]:
    """列出可用摄像头。

    返回 ``[{'index': 0, 'name': '4K AutoFocus Webcam', 'probed': False}, ...]``

    * ``name`` 为空字符串表示只探到序号、拿不到名字；
    * ``probed=True`` 表示这一条是逐 index 探测出来的（没装 ffmpeg）。
    """
    now = time.time()
    if use_cache and _cache['items'] and now - float(_cache['at']) < _CACHE_TTL:
        return list(_cache['items'])                     # type: ignore[arg-type]

    names = _names_from_ffmpeg(find_ffmpeg())
    items: List[Dict[str, object]] = [
        {'index': i, 'name': name, 'probed': False} for i, name in enumerate(names)]

    if not items and probe:
        items = [{'index': i, 'name': '', 'probed': True}
                 for i in _probe_indices(max_index)]

    _cache['at'] = now
    _cache['items'] = list(items)
    return items


def clear_cache() -> None:
    _cache['at'] = 0.0
    _cache['items'] = []


def display_name(item: Dict[str, object]) -> str:
    """下拉框里显示的文字：'0 · 4K AutoFocus Webcam'。"""
    index = item.get('index', 0)
    name = str(item.get('name') or '').strip()
    if not name:
        name = '摄像头 {}（探测）'.format(index) if item.get('probed') else '摄像头 {}'.format(index)
    return '{} · {}'.format(index, name)


def resolve_index(items: List[Dict[str, object]], wanted_index: Optional[int] = None,
                  wanted_name: str = '') -> int:
    """在一份枚举结果里挑出该选哪个 index。

    优先按名字匹配（插拔后序号会漂，名字不会），其次按序号，最后取第一个。
    """
    if wanted_name:
        for item in items:
            if str(item.get('name') or '') == wanted_name:
                return int(item['index'])                # type: ignore[arg-type]
    if wanted_index is not None:
        for item in items:
            if int(item['index']) == int(wanted_index):  # type: ignore[arg-type]
                return int(wanted_index)
    if items:
        return int(items[0]['index'])                    # type: ignore[arg-type]
    return int(wanted_index or 0)


# ---------------------------------------------------------------- 偏好读写
def load_choice() -> Dict[str, object]:
    """读上次选的摄像头：{'index': 0, 'name': '...'}；没有/坏了返回空字典。"""
    try:
        with open(PREFS_PATH, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {'index': int(data.get('index', 0) or 0),
                    'name': str(data.get('name') or '')}
    except Exception:                                     # noqa: BLE001 - 读不到就用默认
        pass
    return {}


def save_choice(index: int, name: str = '') -> None:
    """记住这次的选择。写不进去也不能影响采集，所以异常一律吞掉。"""
    try:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, 'w', encoding='utf-8') as fh:
            json.dump({'index': int(index), 'name': str(name or '')}, fh,
                      ensure_ascii=False, indent=2)
    except Exception:                                     # noqa: BLE001
        pass


if __name__ == '__main__':
    for _item in list_cameras():
        print(display_name(_item))
    print('偏好：', load_choice() or '（未设置）')
