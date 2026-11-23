# -*- coding: utf-8 -*-
"""统一的路径解析：源码运行和 PyInstaller 打包后用同一套相对位置。

两条线要分清楚：

| 目录 | 源码运行 | 打包后 | 是否可写 |
|---|---|---|---|
| 只读资源（models / ui/assets / config.ini 模板 / ppg_pipeline 源码） | AAAwindows_code/ | `sys._MEIPASS`（_internal/） | 否 |
| 运行数据（data/、config.ini、日志） | AAAwindows_code/ | **exe 所在目录** | 是 |

这样打出来的包可以整个文件夹拷到别的电脑、别的位置直接跑：所有路径都由
exe 位置推导，没有任何写死的绝对路径。

对外：
    APP_DIR / BUNDLE_DIR / DATA_DIR / CONFIG_PATH
    app_path(*parts)            可写目录下的路径
    resource(*parts)            只读资源目录下的路径
    resolve_resource_dir(name)  优先 exe 旁边的同名目录，其次打包资源
    ensure_config()             首次运行时把 config.ini 复制到 exe 旁边
"""
from __future__ import annotations

import os
import shutil
import sys

FROZEN = bool(getattr(sys, 'frozen', False))

if FROZEN:
    # 打包后：sys.executable 是 exe 本身，_MEIPASS 是解包出来的只读资源目录
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    # 源码运行：本文件在 AAAwindows_code/core/ 下
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    APP_DIR = BUNDLE_DIR

DATA_DIR = os.path.join(APP_DIR, 'data')
CONFIG_PATH = os.path.join(APP_DIR, 'config.ini')


def app_path(*parts: str) -> str:
    """exe 旁边（可写）目录下的路径。"""
    return os.path.join(APP_DIR, *parts)


def resource(*parts: str) -> str:
    """打包进去的只读资源路径。"""
    return os.path.join(BUNDLE_DIR, *parts)


def resolve_resource_dir(name: str) -> str:
    """优先用 exe 旁边的同名目录（现场替换模型/管线用），没有再退回打包资源。"""
    local = app_path(name)
    if os.path.isdir(local):
        return local
    bundled = resource(name)
    if os.path.isdir(bundled):
        return bundled
    return local


def ensure_config() -> str:
    """打包版第一次运行时，把 config.ini 从只读资源复制到 exe 旁边。

    现场要改设备名、阈值，改 exe 旁边这份就行；打包资源里的那份不动。
    """
    if os.path.exists(CONFIG_PATH):
        return CONFIG_PATH
    source = resource('config.ini')
    if os.path.exists(source):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            shutil.copyfile(source, CONFIG_PATH)
        except OSError:
            return source            # 只读介质（U 盘写保护）时退回用包内那份
    return CONFIG_PATH


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
