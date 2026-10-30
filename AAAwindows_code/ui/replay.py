# -*- coding: utf-8 -*-
"""回放：把已录到的 SQLite / JSONL 当成实时数据喂给界面。

用途有两个：
  1. 没有硬件时开发/验证界面（截图自检、看历史会话）；
  2. 事后复盘某一段采集（按原始时间节奏回放，可倍速）。

数据库是「每个时间戳一组行」存的，这里按时间戳把行重新拼回「一包」：
心电 80 点 + 呼吸 8 点 + 三轴 16 点 + 体温；手环 PPG 10 点 + 三轴 / GSR 10 点 / 肤温 / 电量。
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ui.buffers import LiveBuffers

TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'

ECG_TABLES = {
    'ecg': 'ecg_data',
    'resp': 'breath_data',
    'accel': 'triaxial_data',
    'temp': 'temperature_data',
}
BRACELET_TABLES = {
    'ppg': 'bracelet_ppg_data',
    'accel': 'bracelet_accel_data',
    'gsr': 'bracelet_gsr_data',
    'temp': 'bracelet_skin_temp',
    'battery': 'bracelet_battery',
}


def _to_epoch(text: str) -> float:
    try:
        return datetime.strptime(text, TIME_FORMAT).timestamp()
    except ValueError:
        return time.time()


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def load_camera_rows(db_path: str) -> List[tuple]:
    """读一个库里的摄像头帧 [(time, frame_index, jpeg)]；没有表返回空。"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        if 'camera_frames' not in _tables(conn):
            return []
        return conn.execute(
            'SELECT time, frame_index, jpeg FROM camera_frames ORDER BY frame_index'
        ).fetchall()
    finally:
        conn.close()


def find_camera_companion(db_path: str, max_seconds: float = 900.0) -> Optional[str]:
    """老版本把摄像头单独存成 camera_*.db；按文件名时间找最近的那个配对。"""
    folder = os.path.dirname(os.path.abspath(db_path))
    stem = os.path.splitext(os.path.basename(db_path))[0]
    try:
        base_time = datetime.strptime(stem.replace('camera_', ''), '%Y%m%d_%H%M%S')
    except ValueError:
        base_time = datetime.fromtimestamp(os.path.getmtime(db_path))

    best = None
    for path in glob.glob(os.path.join(folder, 'camera_*.db')):
        name = os.path.splitext(os.path.basename(path))[0].replace('camera_', '')
        try:
            candidate_time = datetime.strptime(name, '%Y%m%d_%H%M%S')
        except ValueError:
            candidate_time = datetime.fromtimestamp(os.path.getmtime(path))
        delta = abs((candidate_time - base_time).total_seconds())
        if delta <= max_seconds and (best is None or delta < best[0]):
            best = (delta, path)
    return best[1] if best else None


def _load_groups(conn: sqlite3.Connection, table: str) -> Dict[str, List[Any]]:
    """按时间戳分组读取 (time, value)，value 尝试按 JSON 解析。"""
    groups: Dict[str, List[Any]] = {}
    if table not in _tables(conn):
        return groups
    for ts, value in conn.execute(f'SELECT time, value FROM "{table}" ORDER BY rowid'):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = value
        groups.setdefault(ts, []).append(parsed)
    return groups


def load_db_packets(db_path: str, limit_seconds: Optional[float] = None
                    ) -> List[Tuple[float, str, Dict[str, Any]]]:
    """从采集数据库重建 [(epoch, 'ecg'|'bracelet', decoded)]。"""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        ecg_groups = _load_groups(conn, ECG_TABLES['ecg'])
        resp_groups = _load_groups(conn, ECG_TABLES['resp'])
        accel_groups = _load_groups(conn, ECG_TABLES['accel'])
        temp_groups = _load_groups(conn, ECG_TABLES['temp'])
        ppg_groups = _load_groups(conn, BRACELET_TABLES['ppg'])
        baccel_groups = _load_groups(conn, BRACELET_TABLES['accel'])
        gsr_groups = _load_groups(conn, BRACELET_TABLES['gsr'])
        btemp_groups = _load_groups(conn, BRACELET_TABLES['temp'])
        battery_groups = _load_groups(conn, BRACELET_TABLES['battery'])
        tables = _tables(conn)
        camera_rows = []
        if 'camera_frames' in tables:
            camera_rows = conn.execute(
                'SELECT time, frame_index, jpeg FROM camera_frames ORDER BY frame_index'
            ).fetchall()
    finally:
        conn.close()

    # 兼容旧数据：老版本把帧单独存在 camera_YYYYmmdd_HHMMSS.db 里，
    # 这里按时间就近配对，让一次回放也能同时拿到摄像头画面
    if not camera_rows:
        companion = find_camera_companion(db_path)
        if companion:
            camera_rows = load_camera_rows(companion)
            if camera_rows:
                print('[回放] 会话库没有摄像头帧，自动配对 {}'.format(os.path.basename(companion)),
                      flush=True)

    packets: List[Tuple[float, str, Dict[str, Any]]] = []
    for index, (ts, samples) in enumerate(sorted(ecg_groups.items())):
        decoded: Dict[str, Any] = {
            'ECG': {'ECG': [float(v) for v in samples]},
            '_包号': index,
        }
        if temp_groups.get(ts):
            decoded['ECG']['体温'] = float(temp_groups[ts][0])
        if resp_groups.get(ts):
            decoded['呼吸'] = [float(v) for v in resp_groups[ts]]
        if accel_groups.get(ts):
            decoded['三轴'] = accel_groups[ts]
        packets.append((_to_epoch(ts), 'ecg', decoded))

    for ts, samples in sorted(ppg_groups.items()):
        decoded = {'PPG': samples}
        if baccel_groups.get(ts):
            decoded['腕部三轴'] = baccel_groups[ts][0]
        packets.append((_to_epoch(ts), 'bracelet', decoded))
    for ts, samples in sorted(gsr_groups.items()):
        packets.append((_to_epoch(ts), 'bracelet', {'GSR': samples}))
    for ts, values in sorted(btemp_groups.items()):
        packets.append((_to_epoch(ts), 'bracelet', {'肤温': values[0]}))
    for ts, values in sorted(battery_groups.items()):
        packets.append((_to_epoch(ts), 'bracelet', {'电量': values[0]}))
    for ts, frame_index, jpeg in camera_rows:
        packets.append((_to_epoch(ts), 'camera',
                        {'jpeg': jpeg, 'frame_index': frame_index, 'time': ts}))

    packets.sort(key=lambda item: item[0])
    if limit_seconds and packets:
        cutoff = packets[0][0] + limit_seconds
        packets = [item for item in packets if item[0] <= cutoff]
    return packets


def load_jsonl_packets(path: str, limit_seconds: Optional[float] = None
                       ) -> List[Tuple[float, str, Dict[str, Any]]]:
    """从 JSONL（每行 {'time','device','data'}）重建心电包。"""
    packets = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            packets.append((_to_epoch(record.get('time', '')), 'ecg', record.get('data') or {}))
    if limit_seconds and packets:
        cutoff = packets[0][0] + limit_seconds
        packets = [item for item in packets if item[0] <= cutoff]
    return packets


def load_packets(path: str, limit_seconds: Optional[float] = None):
    if path.lower().endswith('.jsonl'):
        return load_jsonl_packets(path, limit_seconds)
    return load_db_packets(path, limit_seconds)


def list_sessions(data_dir: str) -> List[Tuple[str, float, int]]:
    """列出数据目录里的回放源：[(路径, 修改时间, 大小)]，新的在前。"""
    items = []
    for pattern in ('*.db', '*.jsonl'):
        for path in glob.glob(os.path.join(data_dir, pattern)):
            try:
                items.append((path, os.path.getmtime(path), os.path.getsize(path)))
            except OSError:
                continue
    items.sort(key=lambda item: item[1], reverse=True)
    return items


class ReplayPlayer(threading.Thread):
    """后台线程：按原始时间节奏把包喂进 LiveBuffers。"""

    def __init__(self, packets, buffers: LiveBuffers, speed: float = 1.0, loop: bool = False):
        super().__init__(name='replay-player', daemon=True)
        self.packets = packets
        self.buffers = buffers
        self.speed = max(0.05, float(speed))
        self.loop = loop
        # 注意：不能叫 _stop，threading.Thread 内部有 _stop() 方法，
        # 重名会让 is_alive() 抛 TypeError: 'Event' object is not callable
        self._stop_event = threading.Event()
        self.position = 0
        self.total = len(packets)
        self.duration = (packets[-1][0] - packets[0][0]) if packets else 0.0

    def progress(self) -> Tuple[int, int, float]:
        """返回 (已回放包数, 总包数, 0~1 进度)。"""
        total = max(1, self.total)
        return self.position, self.total, min(1.0, (self.position + 1) / total)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self.packets:
            return
        while not self._stop_event.is_set():
            base_ts = self.packets[0][0]
            wall_start = time.monotonic()
            for index, (ts, kind, decoded) in enumerate(self.packets):
                self.position = index
                if self._stop_event.is_set():
                    return
                target = wall_start + (ts - base_ts) / self.speed
                while True:
                    remaining = target - time.monotonic()
                    if remaining <= 0:
                        break
                    if self._stop_event.wait(min(0.05, remaining)):
                        return
                if kind == 'ecg':
                    self.buffers.publish_ecg(decoded)
                elif kind == 'bracelet':
                    self.buffers.publish_bracelet(decoded)
                elif kind == 'camera':
                    self.buffers.publish_camera_frame(
                        decoded.get('jpeg', b''), decoded.get('frame_index', 0),
                        decoded.get('time', ''))
            if not self.loop:
                return
            self._stop_event.wait(0.5)
