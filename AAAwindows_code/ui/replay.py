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

from core.db_storage import db_total_size, is_empty_db
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

# 回放内容清单：key -> (界面显示名, 表名, 分组)
# 界面按这个顺序把库里实际存在的表列出来，让用户勾选这次只回放哪几路。
# 加新表时在这里补一行即可，`load_db_packets` 和界面都不用再改。
REPLAY_SOURCES: List[Tuple[str, str, str, str]] = [
    ('ecg',     '心电',        ECG_TABLES['ecg'],          '心电贴'),
    ('resp',    '呼吸',        ECG_TABLES['resp'],         '心电贴'),
    ('accel',   '三轴',        ECG_TABLES['accel'],        '心电贴'),
    ('temp',    '体温',        ECG_TABLES['temp'],         '心电贴'),
    ('ppg',     'PPG',         BRACELET_TABLES['ppg'],     '手环'),
    ('b_accel', '腕部三轴',     BRACELET_TABLES['accel'],   '手环'),
    ('gsr',     '皮电 GSR',     BRACELET_TABLES['gsr'],     '手环'),
    ('b_temp',  '肤温',        BRACELET_TABLES['temp'],    '手环'),
    ('battery', '电量',        BRACELET_TABLES['battery'], '手环'),
    ('camera',  '摄像头帧',     'camera_frames',            '摄像头'),
]

ALL_SOURCE_KEYS = [item[0] for item in REPLAY_SOURCES]


def list_db_sources(db_path: str) -> List[Tuple[str, str, str, int]]:
    """列出某个会话库里实际有数据的回放项 [(key, 分组, 名称, 行数)]。

    行数为 0 的表不返回 —— 空表勾上也没内容，列出来只会让人以为坏了。
    """
    result: List[Tuple[str, str, str, int]] = []
    if not db_path or db_path.lower().endswith('.jsonl'):
        return result
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    except sqlite3.Error:
        return result
    try:
        present = set(_tables(conn))
        for key, name, table, group in REPLAY_SOURCES:
            if table not in present:
                continue
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                continue
            if count:
                result.append((key, group, name, int(count)))
        has_camera = any(item[0] == 'camera' for item in result)
    finally:
        conn.close()

    # 老数据把帧单独存在 camera_*.db 里，本库没有 camera_frames。
    # 回放时能自动配对，所以这里也要把「摄像头帧」列出来，否则用户以为不能选画面。
    if not has_camera:
        companion = find_camera_companion(db_path)
        if companion:
            try:
                frames = len(load_camera_rows(companion))
            except Exception:                             # noqa: BLE001
                frames = 0
            if frames:
                result.append(('camera', '摄像头', '配对录像', frames))
    return result


def _to_epoch(text: str) -> float:
    try:
        return datetime.strptime(text, TIME_FORMAT).timestamp()
    except ValueError:
        return time.time()


# 设备 -> 由哪些表构成。
# 交集口径按「设备」算，不按表：一台设备只要有任意一路有数据就算在线，
# 它的在线区间取这些表的并集（最早开始 ~ 最晚结束）。
# 为什么按设备：手环是分多包发的，PPG / GSR / 电量的起止能差几十秒，
# 按表求交几乎选不出任何一段；按设备才是"戴上了、连上了"这个语义。
INSTRUMENTS: List[Tuple[str, str, List[str]]] = [
    ('ecg', '心电贴', [ECG_TABLES['ecg'], ECG_TABLES['resp'],
                     ECG_TABLES['accel'], ECG_TABLES['temp']]),
    ('bracelet', '手环', [BRACELET_TABLES['ppg'], BRACELET_TABLES['accel'],
                        BRACELET_TABLES['gsr'], BRACELET_TABLES['temp'],
                        BRACELET_TABLES['battery']]),
    ('camera', '摄像头', ['camera_frames']),
]

INSTRUMENT_NAMES = {key: name for key, name, _tables in INSTRUMENTS}


def _span_of(conn: sqlite3.Connection, table: str) -> Tuple[Optional[float], Optional[float], int]:
    """一张表的 (首个时间, 末个时间, 行数)；表不存在或为空返回 (None, None, 0)。"""
    try:
        lo, hi, count = conn.execute(
            'SELECT MIN(time), MAX(time), COUNT(*) FROM "%s"' % table).fetchone()
    except sqlite3.Error:
        return None, None, 0
    if not count or not lo or not hi:
        return None, None, 0
    return _to_epoch(lo), _to_epoch(hi), int(count)


def instrument_spans(db_path: str) -> List[Dict[str, Any]]:
    """每台设备在本会话里的在线区间。

    返回 [{key, name, start, end, rows, tables}]，start/end 是 epoch 秒。
    没有数据的设备不返回；摄像头若在本库没表，会去配对的 camera_*.db 取。
    """
    spans: List[Dict[str, Any]] = []
    if not db_path or db_path.lower().endswith('.jsonl'):
        return spans

    def scan(path: str) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        try:
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        except sqlite3.Error:
            return found
        try:
            # 这里不用模块级的 _tables()：它在文件后面才定义，嵌套函数引用会踩
            # Python 3.12 的 free-variable 解析问题，直接查一次更省事
            present = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for key, name, tables in INSTRUMENTS:
                usable = [table for table in tables if table in present]
                if not usable:
                    continue
                first = last = None
                rows = 0
                for table in usable:
                    start, end, count = _span_of(conn, table)
                    if not count:
                        continue
                    rows += count
                    first = start if first is None else min(first, start)
                    last = end if last is None else max(last, end)
                if rows:
                    found[key] = {'key': key, 'name': name, 'start': first,
                                  'end': last, 'rows': rows, 'tables': usable}
        finally:
            conn.close()
        return found

    found = scan(db_path)
    # 老数据把帧单独存 camera_*.db，本库没有 camera_frames —— 回放能自动配对，
    # 这里也要算上，否则「所有设备同一段」会永远判成摄像头缺失。
    if 'camera' not in found:
        companion = find_camera_companion(db_path)
        if companion:
            found.update({k: v for k, v in scan(companion).items() if k == 'camera'})
    for key, _name, _tables in INSTRUMENTS:      # 按固定顺序返回，界面不用再排
        if key in found:
            spans.append(found[key])
    return spans


def overlap_window(db_path: str, keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """所有（选中的）设备**同时在线**的时间窗。

    起点取各设备 start 的最大值，终点取各设备 end 的最小值。
    交集为空时 start/end 返回 None —— 调用方必须如实提示，不能给一个 0 长度区间。
    """
    spans = instrument_spans(db_path)
    by_key = {item['key']: item for item in spans}
    wanted = list(keys) if keys else [key for key, _n, _t in INSTRUMENTS]
    chosen = [by_key[key] for key in wanted if key in by_key]
    missing = [INSTRUMENT_NAMES.get(key, key) for key in wanted if key not in by_key]

    result: Dict[str, Any] = {
        'start': None, 'end': None, 'duration': 0.0,
        'devices': [item['name'] for item in chosen],
        'missing': missing, 'spans': spans, 'ok': False,
    }
    if not chosen:
        return result

    start = max(item['start'] for item in chosen)
    end = min(item['end'] for item in chosen)
    result['start'], result['end'] = start, end
    result['duration'] = max(0.0, end - start)
    result['ok'] = end > start
    if not result['ok']:
        result['start'] = result['end'] = None
    return result


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


def load_db_packets(db_path: str, limit_seconds: Optional[float] = None,
                    include: Optional[Any] = None,
                    time_range: Optional[Tuple[float, float]] = None
                    ) -> List[Tuple[float, str, Dict[str, Any]]]:
    """从采集数据库重建 [(epoch, 'ecg'|'bracelet'|'camera', decoded)]。

    `include` 指定这次要哪些内容（REPLAY_SOURCES 的 key 集合）。
    传空集合就是什么都不载；传 None 表示全部。
    """
    want = set(ALL_SOURCE_KEYS) if include is None else set(include)
    empty: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        ecg_groups = _load_groups(conn, ECG_TABLES['ecg']) if 'ecg' in want else empty
        resp_groups = _load_groups(conn, ECG_TABLES['resp']) if 'resp' in want else empty
        accel_groups = _load_groups(conn, ECG_TABLES['accel']) if 'accel' in want else empty
        temp_groups = _load_groups(conn, ECG_TABLES['temp']) if 'temp' in want else empty
        ppg_groups = _load_groups(conn, BRACELET_TABLES['ppg']) if 'ppg' in want else empty
        baccel_groups = _load_groups(conn, BRACELET_TABLES['accel']) if 'b_accel' in want else empty
        gsr_groups = _load_groups(conn, BRACELET_TABLES['gsr']) if 'gsr' in want else empty
        btemp_groups = _load_groups(conn, BRACELET_TABLES['temp']) if 'b_temp' in want else empty
        battery_groups = (_load_groups(conn, BRACELET_TABLES['battery'])
                          if 'battery' in want else empty)
        tables = _tables(conn)
        camera_rows = []
        if 'camera' in want and 'camera_frames' in tables:
            camera_rows = conn.execute(
                'SELECT time, frame_index, jpeg FROM camera_frames ORDER BY frame_index'
            ).fetchall()
    finally:
        conn.close()

    # 兼容旧数据：老版本把帧单独存在 camera_YYYYmmdd_HHMMSS.db 里，
    # 这里按时间就近配对，让一次回放也能同时拿到摄像头画面
    if not camera_rows and 'camera' in want:
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
    if time_range:
        start, end = time_range
        packets = [item for item in packets if start <= item[0] <= end]
    if limit_seconds and packets:
        cutoff = packets[0][0] + limit_seconds
        packets = [item for item in packets if item[0] <= cutoff]
    return packets


def load_jsonl_packets(path: str, limit_seconds: Optional[float] = None,
                       include: Optional[Any] = None,
                       time_range: Optional[Tuple[float, float]] = None
                       ) -> List[Tuple[float, str, Dict[str, Any]]]:
    """从 JSONL（每行 {'time','device','data'}）重建心电包。"""
    if include is not None and 'ecg' not in set(include):
        return []
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
    if time_range:
        start, end = time_range
        packets = [item for item in packets if start <= item[0] <= end]
    if limit_seconds and packets:
        cutoff = packets[0][0] + limit_seconds
        packets = [item for item in packets if item[0] <= cutoff]
    return packets


def load_yawn_events(db_path: str, start: Optional[float] = None,
                     end: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
    """读会话库里的打哈欠事件。

    返回 None 表示**这个库根本没有 yawn_events 表**（旧版本录的 / 录制时没开检测），
    调用方要据此提示"没有记录"，而不是当成"0 次哈欠"——这两件事含义完全不同。
    表的形状与其它表一致：(time TEXT, value TEXT)，value 是 JSON。
    """
    if not db_path or db_path.lower().endswith('.jsonl'):
        return None
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    except sqlite3.Error:
        return None
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'yawn_events' not in present:
            return None
        query = 'SELECT time, value FROM yawn_events'
        params: Tuple[Any, ...] = ()
        if start is not None and end is not None:
            query += ' WHERE time >= ? AND time <= ?'
            params = (datetime.fromtimestamp(start).strftime(TIME_FORMAT),
                      datetime.fromtimestamp(end).strftime(TIME_FORMAT))
        query += ' ORDER BY time'
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    events: List[Dict[str, Any]] = []
    for stamp, value in rows:
        try:
            payload = json.loads(value) if isinstance(value, str) else dict(value)
        except (TypeError, ValueError):
            payload = {}
        payload.setdefault('time', _to_epoch(stamp))
        events.append(payload)
    return events


def load_packets(path: str, limit_seconds: Optional[float] = None,
                 include: Optional[Any] = None,
                 time_range: Optional[Tuple[float, float]] = None):
    """按内容/时间范围载入回放包。

    `time_range=(起, 止)`（epoch 秒）给定时只载这一段 —— 回放观看页播的就是它。
    """
    if path.lower().endswith('.jsonl'):
        return load_jsonl_packets(path, limit_seconds, include=include,
                                  time_range=time_range)
    return load_db_packets(path, limit_seconds, include=include, time_range=time_range)


def format_size(size: Optional[int]) -> str:
    """人能读的大小。

    以前一律换算成 MB 保留一位小数，几百 KB 的会话也显示成 0.0 MB，
    看起来就像「一堆空库」。这里不足 1 MB 改用 KB 显示。
    """
    if size is None:
        return '—'
    if size >= 1024 ** 3:
        return '{:.2f} GB'.format(size / 1024 ** 3)
    if size >= 1024 ** 2:
        return '{:.1f} MB'.format(size / 1024 ** 2)
    if size >= 1024:
        return '{:.0f} KB'.format(size / 1024)
    return '{} B'.format(int(size))


def list_session_folders(data_dir: str) -> List[Tuple[str, int]]:
    """列出 data/ 下有会话的一级文件夹：[(文件夹名, 会话数)]。

    第一个人是人员姓名目录（`data/张三/任务A_20261103_101010/xxx.db`），
    直接放在 data/ 根下的旧会话归到 ''（界面显示成「未分类」）。
    返回值按"有名字的在前、根目录最后"排序。
    """
    counts: Dict[str, int] = {}
    for root, _dirs, files in os.walk(data_dir):
        found = sum(1 for name in files if name.lower().endswith('.db'))
        if not found:
            continue
        rel = os.path.relpath(root, data_dir)
        top = '' if rel == '.' else rel.replace('\\', '/').split('/')[0]
        counts[top] = counts.get(top, 0) + found
    return sorted(counts.items(), key=lambda item: (item[0] == '', item[0]))


def list_sessions(data_dir: str, recursive: bool = True,
                  skip_empty: bool = True,
                  folder: Optional[str] = None) -> List[Tuple[str, float, int]]:
    """列出数据目录里的回放源：[(路径, 修改时间, 大小)]，新的在前。

    三处口径修正：
      1. 大小按「主库 + -wal + -shm」合计。WAL 模式下已提交的数据可能还在
         -wal 里没 checkpoint 回主库，只看主库会把正在采集的会话显示成 0 MB。
      2. 跳过只有文件头、零张表的空库（程序启动时建出来但没采到数据的那些）。
      3. 递归子目录，`data/<人员姓名>/<任务名>_<时间>/` 这种分层会话也能列出来。
      4. `folder` 给定时只列那个一级文件夹里的（回放页"先选人再选任务"就是靠它）。
    """
    target = os.path.join(data_dir, folder) if folder else data_dir
    items = []
    walker = os.walk(target) if recursive else [(target, [], _listdir(target))]
    for root, _dirs, files in walker:
        for name in files:
            lower = name.lower()
            path = os.path.join(root, name)
            if lower.endswith('.db'):
                if skip_empty and is_empty_db(path):
                    continue
                size = db_total_size(path)
            elif lower.endswith('.jsonl'):
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
            else:
                continue                        # 排除 -wal / -shm / -journal 等附属文件
            try:
                items.append((path, os.path.getmtime(path), size))
            except OSError:
                continue
    items.sort(key=lambda item: item[1], reverse=True)
    return items


def _listdir(path: str) -> List[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


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
