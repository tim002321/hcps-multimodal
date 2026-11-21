# -*- coding: utf-8 -*-
"""文件读写与路径工具。

Windows 上 cv2.imread / imwrite 遇到中文路径会返回 None / 静默失败，
统一走 np.fromfile + imdecode、imencode + tofile。
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Optional

import cv2
import numpy as np

_last_error = ''


def app_root() -> str:
    """程序根目录：源码运行时是 face_tool/，打包后是 exe 所在目录。

    打包产物里不能往 _MEIPASS 写东西（那是临时解压目录，退出即删），
    所以状态文件、默认输出目录一律挂在 exe 旁边。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_root() -> str:
    """只读资源根目录：打包后是 _MEIPASS，源码运行时等同 app_root()。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return app_root()


def last_error() -> str:
    return _last_error


def _set_error(msg: str) -> None:
    global _last_error
    _last_error = msg


def win_long(path: str) -> str:
    """超过 240 字符的绝对路径加 \\\\?\\ 前缀（Windows）。"""
    if sys.platform != 'win32':
        return path
    absolute = os.path.abspath(path)
    if len(absolute) < 240 or absolute.startswith('\\\\?\\'):
        return absolute
    if absolute.startswith('\\\\'):
        return '\\\\?\\UNC\\' + absolute[2:]
    return '\\\\?\\' + absolute


def ensure_dir(path: str) -> None:
    if not path:
        return
    os.makedirs(win_long(path), exist_ok=True)


def imread_unicode(path: str, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """读图，支持中文路径。失败返回 None，原因写进 last_error()。"""
    if not path or not os.path.exists(win_long(path)):
        _set_error('文件不存在：{}'.format(path))
        return None
    try:
        buf = np.fromfile(win_long(path), dtype=np.uint8)
        if buf.size == 0:
            _set_error('文件为空：{}'.format(path))
            return None
        img = cv2.imdecode(buf, flags)
        if img is None:
            _set_error('解码失败：{}'.format(path))
        return img
    except Exception as exc:                       # noqa: BLE001
        _set_error('读取异常 {}：{}'.format(path, exc))
        return None


def imwrite_unicode(path: str, image: np.ndarray, quality: int = 95) -> bool:
    """写图，支持中文路径。按扩展名选编码参数。"""
    if image is None or getattr(image, 'size', 0) == 0:
        _set_error('待写图像为空：{}'.format(path))
        return False
    ext = os.path.splitext(path)[1].lower() or '.jpg'
    if ext in ('.jpg', '.jpeg'):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    elif ext == '.png':
        params = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    else:
        params = []
    try:
        ok, buf = cv2.imencode(ext, image, params)
        if not ok:
            _set_error('编码失败：{}'.format(path))
            return False
        ensure_dir(os.path.dirname(path))
        buf.tofile(win_long(path))
        return True
    except Exception as exc:                       # noqa: BLE001
        _set_error('写入异常 {}：{}'.format(path, exc))
        return False


def free_gb(path: str) -> float:
    """目标盘剩余空间（GB）。路径不存在则向上找最近的已存在目录。"""
    probe = os.path.abspath(path or '.')
    while probe and not os.path.exists(win_long(probe)):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(win_long(probe)).free / (1024 ** 3)
    except Exception:                              # noqa: BLE001
        return -1.0


def open_folder(path: str) -> None:
    if not path or not os.path.isdir(path):
        return
    try:
        os.startfile(path)                         # noqa: S606 - 仅 Windows 本地打开
    except Exception:                              # noqa: BLE001
        pass


def short_path(path: str, limit: int = 70) -> str:
    """过长路径中间省略，给界面显示用。"""
    if len(path) <= limit:
        return path
    tail = os.sep + os.sep.join(path.split(os.sep)[-3:])
    return '…' + tail if len(tail) <= limit else '…' + path[-limit:]


def is_within(child: str, parent: str) -> bool:
    """child 是否在 parent 内部（含相等）。用来拦住"输出放进输入目录"。

    Windows 下大小写与斜杠方向都要归一化，否则 F:\\Data 与 f:/data 判不出来。
    """
    if not child or not parent:
        return False
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
    except (TypeError, ValueError):
        return False
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)
