#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体文件（照片 / 视频）的路径推导。

摄像头帧不再以 BLOB 存进 SQLite，而是落在**会话库所在目录**下：

    data/<姓名>/<任务名>_<时间戳>/
        ├── 20261114_111026.db      会话库（心电/手环/摄像头元数据）
        ├── frames/                 照片  frame_xxx.jpg
        └── video/                  视频  video_xxx.mp4

放在 core/ 是因为 devices、ui、tools 三方都要用；devices 不反向依赖 ui。

边界情况：库直接躺在 data/ 根下（`run_monitor.py --no-profile`、CLI 单跑、
工具自检都会这样），此时若仍然用库所在目录当媒体根，所有会话会共用
`data/frames/`、`data/video/`，互相混在一起。所以那种情况退到
`data/<库名>_media/`，每个会话仍是独立目录。
"""
from __future__ import annotations

import os
from typing import Optional

from core.config import DATA_DIR

FRAMES_DIRNAME = 'frames'
VIDEO_DIRNAME = 'video'
MEDIA_SUFFIX = '_media'


def media_root(db_path: str) -> str:
    """会话库对应的媒体根目录。"""
    path = os.path.abspath(db_path)
    base = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    if os.path.normcase(base) == os.path.normcase(os.path.abspath(DATA_DIR)):
        return os.path.join(base, stem + MEDIA_SUFFIX)
    return base


def frames_dir(db_path: str) -> str:
    """照片目录（懒创建，第一帧才建）。"""
    return os.path.join(media_root(db_path), FRAMES_DIRNAME)


def video_dir(db_path: str) -> str:
    """视频目录（懒创建，第一帧才建）。"""
    return os.path.join(media_root(db_path), VIDEO_DIRNAME)


def frame_path(db_path: str, file_name: str) -> str:
    """把库里的相对文件名还原成绝对路径。"""
    if not file_name:
        return ''
    return os.path.join(frames_dir(db_path), file_name)


def video_path(db_path: str, file_name: str) -> str:
    if not file_name:
        return ''
    return os.path.join(video_dir(db_path), file_name)


def dir_bytes(path: str) -> int:
    """目录下所有文件的字节数之和；目录不存在或读不了按 0 处理。

    只用于界面/工具统计，不追求精确（遍历中文件增删会带来微小误差）。
    """
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def media_bytes(db_path: str) -> int:
    """照片 + 视频的总字节数。"""
    return dir_bytes(frames_dir(db_path)) + dir_bytes(video_dir(db_path))


def session_media_dirs(db_path: str) -> list:
    """要随会话一起删除的媒体目录（不含会话目录本身）。"""
    dirs = []
    root = media_root(db_path)
    for name in (FRAMES_DIRNAME, VIDEO_DIRNAME):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            dirs.append(path)
    # data 根下的库：媒体根是 <库名>_media，删完子目录后整个 _media 也空了
    if os.path.basename(root).endswith(MEDIA_SUFFIX):
        dirs.append(root)
    return dirs
