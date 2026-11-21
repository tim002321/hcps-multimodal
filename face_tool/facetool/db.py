# -*- coding: utf-8 -*-
"""只读访问会话数据库（SQLite）。

规则：
* 一律 mode=ro 打开，绝不写库；WAL 模式下主程序在写时也能读。
* 遍历一律 ORDER BY id —— frame_index 在重开流时会回到 1，按它排序会让时间倒退。
* 兼容两种结构：
    新：camera_frames(id,time,camera,frame_index,width,height,file_name)
    老：camera_frames(id,time,camera,frame_index,width,height,jpeg)   ← 图在库里
"""
from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

TABLE = 'camera_frames'


@dataclass
class DbProbe:
    path: str
    exists: bool = False
    has_table: bool = False
    columns: List[str] = field(default_factory=list)
    row_count: int = 0
    time_min: str = ''
    time_max: str = ''
    error: str = ''

    @property
    def has_file_name(self) -> bool:
        return 'file_name' in self.columns

    @property
    def has_jpeg(self) -> bool:
        return 'jpeg' in self.columns

    @property
    def legacy(self) -> bool:
        """老结构：图直接存在库里。"""
        return self.has_jpeg and not self.has_file_name

    def describe(self) -> str:
        if not self.exists:
            return '无数据库'
        if not self.has_table:
            return '库内无 {} 表'.format(TABLE)
        kind = '老结构（图在库内）' if self.legacy else '新结构（图在 frames/）'
        return '{}，{} 帧'.format(kind, self.row_count)


@dataclass
class DbRow:
    id: int
    time: str
    camera: str
    frame_index: int
    width: int
    height: int
    file_name: str = ''
    jpeg: Optional[bytes] = None


def _connect(path: str) -> sqlite3.Connection:
    uri = 'file:{}?mode=ro'.format(path.replace('\\', '/').replace('?', '%3f'))
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=5000')
    except sqlite3.Error:
        pass
    return conn


def probe_db(path: str) -> DbProbe:
    info = DbProbe(path=path, exists=bool(path) and os.path.exists(path))
    if not info.exists:
        info.error = '库文件不存在'
        return info
    try:
        conn = _connect(path)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            info.has_table = TABLE in names
            if not info.has_table:
                return info
            info.columns = [r[1] for r in conn.execute('PRAGMA table_info({})'.format(TABLE))]
            row = conn.execute(
                'SELECT COUNT(*), MIN(time), MAX(time) FROM {}'.format(TABLE)).fetchone()
            info.row_count = int(row[0] or 0)
            info.time_min = row[1] or ''
            info.time_max = row[2] or ''
        finally:
            conn.close()
    except sqlite3.Error as exc:
        info.error = '读库失败：{}'.format(exc)
    return info


def iter_db_rows(path: str, probe: Optional[DbProbe] = None,
                 with_blob: bool = False) -> Iterator[DbRow]:
    """按 id 遍历。with_blob=False 时不取 jpeg 列（老库上能把内存压住）。"""
    probe = probe or probe_db(path)
    if not probe.exists or not probe.has_table:
        return
    cols = ['id', 'time', 'camera', 'frame_index', 'width', 'height']
    if probe.has_file_name:
        cols.append('file_name')
    if with_blob and probe.has_jpeg:
        cols.append('jpeg')
    sql = 'SELECT {} FROM {} ORDER BY id'.format(','.join(cols), TABLE)
    conn = _connect(path)
    try:
        for row in conn.execute(sql):
            yield DbRow(
                id=int(row['id']),
                time=row['time'] or '',
                camera=row['camera'] or '',
                frame_index=int(row['frame_index'] or 0),
                width=int(row['width'] or 0),
                height=int(row['height'] or 0),
                file_name=(row['file_name'] or '') if probe.has_file_name else '',
                jpeg=bytes(row['jpeg']) if (with_blob and probe.has_jpeg and row['jpeg'])
                else None,
            )
    finally:
        conn.close()


def fetch_blob(path: str, row_id: int) -> Optional[bytes]:
    """按 id 单取一张图（老库分页处理时用，避免一次性载入全部）。"""
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                'SELECT jpeg FROM {} WHERE id=?'.format(TABLE), (int(row_id),)).fetchone()
            return bytes(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def first_size(path: str) -> Tuple[int, int]:
    """首帧的编码尺寸，给界面显示用。取不到返回 (0, 0)。"""
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                'SELECT width, height FROM {} ORDER BY id LIMIT 1'.format(TABLE)).fetchone()
            if row:
                return int(row[0] or 0), int(row[1] or 0)
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return 0, 0


def find_db_files(session_dir: str) -> List[str]:
    """会话目录下的库文件，排除 -wal / -shm。"""
    found = []
    for item in glob.glob(os.path.join(session_dir, '*.db')):
        low = item.lower()
        if low.endswith('-wal') or low.endswith('-shm'):
            continue
        found.append(item)
    return sorted(found)


def pick_best_db(session_dir: str) -> Tuple[Optional[str], List[DbProbe]]:
    """挑一个含有最多帧记录的库；返回 (路径, 全部探测结果)。"""
    probes = [probe_db(p) for p in find_db_files(session_dir)]
    usable = [p for p in probes if p.has_table and p.row_count > 0]
    if not usable:
        return None, probes
    best = max(usable, key=lambda p: p.row_count)
    return best.path, probes
