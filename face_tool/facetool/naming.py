# -*- coding: utf-8 -*-
"""帧文件名：解析与生成，规则与主程序 devices/camera_media.py 一致。

    名 = {prefix}_{seq:06d}.jpg
    prefix = YYYYmmdd_HHMMSS_ffffff        # 22 字符，开流时刻
    例 = 20261117_192821_202189_000001.jpg # 33 字符

注意 prefix 不是每帧时间：同一段采集里所有帧共用一个 prefix，只有 seq 变。
每帧真实时间在数据库 camera_frames.time 里。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional, Tuple

PREFIX_FMT = '%Y%m%d_%H%M%S_%f'
FRAME_RE = re.compile(r'^(?P<prefix>\d{8}_\d{6}_\d{6})_(?P<seq>\d{6})$')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.bmp')

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def parse_stem(stem: str) -> Optional[Tuple[str, int]]:
    """解析主程序格式的文件名（不含扩展名）。返回 (prefix, seq) 或 None。"""
    match = FRAME_RE.match(stem or '')
    if not match:
        return None
    return match.group('prefix'), int(match.group('seq'))


def parse_name(name: str) -> Optional[Tuple[str, int, str]]:
    """解析完整文件名，返回 (prefix, seq, ext)。"""
    stem, ext = os.path.splitext(os.path.basename(name or ''))
    parsed = parse_stem(stem)
    if parsed is None:
        return None
    return parsed[0], parsed[1], ext.lower()


def prefix_to_datetime(prefix: str) -> Optional[datetime]:
    try:
        return datetime.strptime(prefix, PREFIX_FMT)
    except (TypeError, ValueError):
        return None


def synthetic_prefix(db_stem: str) -> str:
    """老库没有 file_name 时，用库名补一个 22 字符前缀。

    '20261103_142134' -> '20261103_142134_000000'
    """
    digits = re.sub(r'\D', '', db_stem or '')
    if len(digits) >= 14:
        return '{}_{}_000000'.format(digits[:8], digits[8:14])
    return '{}_{:06d}'.format(datetime.now().strftime('%Y%m%d_%H%M%S'), 0)


def render_name(prefix: str, seq: int, ext: str = '.jpg') -> str:
    """按主程序规则拼文件名。"""
    ext = ext if ext.startswith('.') else '.' + ext
    return '{}_{:06d}{}'.format(prefix, int(seq), ext.lower())


def new_prefix(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now()).strftime(PREFIX_FMT)


def safe_filename(name: str, fallback: str = 'frame') -> str:
    """去掉文件名里的非法字符（留给外来图片用）。"""
    stem, ext = os.path.splitext(os.path.basename(name or ''))
    stem = _BAD_CHARS.sub('_', stem).strip(' .') or fallback
    ext = ext.lower() if ext.lower() in IMAGE_EXTS else '.jpg'
    return stem + ext


def is_image(name: str) -> bool:
    return os.path.splitext(name or '')[1].lower() in IMAGE_EXTS


def default_ext(name: str) -> str:
    ext = os.path.splitext(name or '')[1].lower()
    return ext if ext in IMAGE_EXTS else '.jpg'
