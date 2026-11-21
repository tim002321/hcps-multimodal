# -*- coding: utf-8 -*-
"""会话发现：从用户选的目录里找出「要处理哪些图」。

三种情况：
1. 目录里有 frames/         → 正常会话，配合 *.db 拿每帧时间
2. 目录本身就是图片目录      → 只有图，没有时间轴
3. 老会话（图在库里的 BLOB） → 从库取图，用库名合成前缀
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from . import db as db_mod
from . import naming

TIME_FMT = '%Y-%m-%d %H:%M:%S.%f'
FRAMES_DIRNAME = 'frames'


@dataclass
class FrameRef:
    """一帧的引用：要么有文件路径，要么有内存里的字节。"""
    name: str                       # 输出用的文件名（与源帧一致）
    path: str = ''                  # 源文件绝对路径（老库为空）
    blob: Optional[bytes] = None    # 老库里直接取出的 jpeg
    time: str = ''                  # 'YYYY-mm-dd HH:MM:SS.ffffff'
    frame_index: int = 0
    camera: str = ''
    db_id: int = 0
    time_source: str = 'empty'      # db | synthesized | empty

    @property
    def has_file(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    def time_epoch(self) -> Optional[float]:
        if not self.time:
            return None
        for fmt in (TIME_FMT, '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(self.time, fmt).timestamp()
            except ValueError:
                continue
        return None


@dataclass
class SessionInfo:
    session_dir: str = ''
    frames_dir: str = ''
    db_path: str = ''
    db_kind: str = ''               # new | legacy | none
    probe: Optional[db_mod.DbProbe] = None
    frames: List[FrameRef] = field(default_factory=list)
    width: int = 0
    height: int = 0
    duration_s: float = 0.0
    prefix_counts: dict = field(default_factory=dict)
    orphan_files: int = 0           # frames/ 里有、库里没有的图
    problem: str = ''
    rel_path: str = ''              # 相对批量根的路径，输出树按它镜像

    @property
    def total(self) -> int:
        return len(self.frames)

    @property
    def name(self) -> str:
        return os.path.basename(os.path.normpath(self.session_dir)) or 'session'

    def prefixes(self) -> List[str]:
        return sorted(self.prefix_counts)

    def describe(self) -> str:
        if self.problem:
            return self.problem
        parts = ['{} 帧'.format(self.total)]
        if self.prefix_counts:
            parts.append('{} 段前缀'.format(len(self.prefix_counts)))
        if self.width and self.height:
            parts.append('{}×{}'.format(self.width, self.height))
        if self.duration_s > 0:
            parts.append('{:.1f} 秒'.format(self.duration_s))
        if self.orphan_files:
            parts.append('{} 张无库记录'.format(self.orphan_files))
        return ' ｜ '.join(parts)


def _image_files(folder: str) -> List[str]:
    from glob import glob
    out: List[str] = []
    for ext in ('*.jpg', '*.jpeg', '*.png', '*.bmp'):
        out.extend(glob(os.path.join(folder, ext)))
        out.extend(glob(os.path.join(folder, ext.upper())))
    return sorted(set(out))


def discover_session(folder: str, include_orphans: bool = True) -> SessionInfo:
    """扫描目录，返回可处理的帧列表。不加载任何模型，纯 IO。"""
    info = SessionInfo(session_dir=os.path.abspath(folder or ''))
    if not info.session_dir or not os.path.isdir(info.session_dir):
        info.problem = '目录不存在：{}'.format(folder)
        return info

    frames_sub = os.path.join(info.session_dir, FRAMES_DIRNAME)
    info.frames_dir = frames_sub if os.path.isdir(frames_sub) else info.session_dir

    db_path, probes = db_mod.pick_best_db(info.session_dir)
    info.db_path = db_path or ''
    info.probe = next((p for p in probes if p.path == db_path), None) if db_path else (
        probes[0] if probes else None)

    on_disk = _image_files(info.frames_dir)
    disk_by_name = {os.path.basename(p): p for p in on_disk}

    # ---- 情况 3：老库，图在 BLOB 里 ----
    if info.probe is not None and info.probe.legacy:
        info.db_kind = 'legacy'
        prefix = naming.synthetic_prefix(
            os.path.splitext(os.path.basename(info.probe.path))[0])
        for row in db_mod.iter_db_rows(info.probe.path, info.probe, with_blob=False):
            ref = FrameRef(
                name=naming.render_name(prefix, row.frame_index or row.id),
                time=row.time, frame_index=row.frame_index, camera=row.camera,
                db_id=row.id, time_source='db')
            info.frames.append(ref)
        info.orphan_files = len(on_disk)
        _fill_meta(info)
        return info

    # ---- 情况 1：新会话，库里有 file_name ----
    if info.probe is not None and info.probe.has_file_name and info.probe.row_count:
        info.db_kind = 'new'
        for row in db_mod.iter_db_rows(info.probe.path, info.probe):
            name = row.file_name or ''
            if not name:
                continue
            path = disk_by_name.get(name, os.path.join(info.frames_dir, name))
            if not os.path.exists(path):
                if not include_orphans:
                    continue
                path = ''
            ref = FrameRef(name=name, path=path, time=row.time,
                           frame_index=row.frame_index, camera=row.camera,
                           db_id=row.id, time_source='db')
            info.frames.append(ref)
        used = {f.name for f in info.frames}
        extra = [n for n in disk_by_name if n not in used]
        info.orphan_files = len(extra)
        if include_orphans:
            for name in sorted(extra):
                info.frames.append(FrameRef(name=name, path=disk_by_name[name],
                                            time_source='empty'))
        _fill_meta(info)
        return info

    # ---- 情况 2：只有图片 ----
    info.db_kind = 'none' if not info.probe else 'empty'
    for path in on_disk:
        name = os.path.basename(path)
        parsed = naming.parse_name(name)
        info.frames.append(FrameRef(
            name=name, path=path,
            time=naming.prefix_to_datetime(parsed[0]).strftime(TIME_FMT) if parsed
            and naming.prefix_to_datetime(parsed[0]) else '',
            time_source='synthesized' if parsed else 'empty'))
    _fill_meta(info)
    if not info.frames:
        info.problem = '目录里没有图片：{}'.format(info.frames_dir)
    return info


def _fill_meta(info: SessionInfo) -> None:
    counts = {}
    for ref in info.frames:
        parsed = naming.parse_stem(os.path.splitext(ref.name)[0])
        key = parsed[0] if parsed else '(非标准名)'
        counts[key] = counts.get(key, 0) + 1
    info.prefix_counts = counts

    if info.probe is not None and info.probe.has_table:
        info.width, info.height = db_mod.first_size(info.probe.path)

    epochs = [r.time_epoch() for r in info.frames]
    valid = [t for t in epochs if t]
    if len(valid) >= 2:
        info.duration_s = max(valid) - min(valid)


# ---------------------------------------------------------------- 批量发现
@dataclass
class SessionEntry:
    """轻量条目：只够画列表和估进度，真正的 frames 列表在轮到它时才构建。"""
    dir: str
    rel: str            # 相对批量根
    kind: str           # frames | db | images
    est_frames: int = 0


def discover_sessions(root: str, max_depth: int = 6,
                      min_images: int = 5) -> List[SessionEntry]:
    """在一个大目录下递归找出所有会话目录。

    判定优先级：含 frames/ > 含会话库 > 直接含若干图片。
    命中后不再深入该子树，避免把 frames/ 里的图当成独立会话。

    另外会跳过"本工具的产物目录"（含 eyes_left / eyes_right / landmarks_check），
    这样即使输出被放在输入根目录里，也不会出现"眼睛的眼睛"。
    """
    root = os.path.abspath(root or '')
    if not os.path.isdir(root):
        return []

    entries: List[SessionEntry] = []
    for current, dirs, files in os.walk(root):
        depth = current[len(root):].strip(os.sep).count(os.sep) + (0 if current == root else 1)
        if depth > max_depth:
            dirs[:] = []
            continue

        if looks_like_output(current, dirs):
            dirs[:] = []
            continue

        has_frames = 'frames' in dirs and os.path.isdir(os.path.join(current, 'frames'))
        dbs = [f for f in files if f.lower().endswith('.db')]

        if has_frames:
            est = _count_images(os.path.join(current, 'frames'))
            entries.append(SessionEntry(current, _rel(current, root), 'frames', est))
            dirs[:] = []
            continue

        if dbs:
            est = _estimate_from_db(os.path.join(current, dbs[0]))
            entries.append(SessionEntry(current, _rel(current, root), 'db', est))
            dirs[:] = []
            continue

        imgs = [f for f in files if naming.is_image(f)]
        if len(imgs) >= min_images:
            entries.append(SessionEntry(current, _rel(current, root), 'images', len(imgs)))

    entries.sort(key=lambda e: e.rel.lower())
    return entries


OUTPUT_MARKERS = ('eyes_left', 'eyes_right', 'landmarks_check')


def looks_like_output(folder: str, subdirs: Optional[List[str]] = None) -> bool:
    """目录里有没有本工具留下的结构，有就说明这是产物、不是原始数据。"""
    names = subdirs if subdirs is not None else [
        d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))] \
        if os.path.isdir(folder) else []
    lowered = {name.lower() for name in names}
    if any(marker in lowered for marker in OUTPUT_MARKERS):
        return True
    for flag in ('_batch_summary.csv', 'run.json'):
        if os.path.exists(os.path.join(folder, flag)):
            return True
    return False


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return os.path.basename(path)


def _count_images(folder: str) -> int:
    try:
        return sum(1 for f in os.listdir(folder) if naming.is_image(f))
    except OSError:
        return 0


def _estimate_from_db(db_path: str) -> int:
    probe = db_mod.probe_db(db_path)
    return probe.row_count if probe.has_table else 0


def looks_like_session(folder: str) -> bool:
    """单目录判断：GUI 用它决定走单会话还是批量。"""
    if not os.path.isdir(folder):
        return False
    if os.path.isdir(os.path.join(folder, FRAMES_DIRNAME)):
        return True
    if db_mod.find_db_files(folder):
        return True
    return _count_images(folder) >= 5
