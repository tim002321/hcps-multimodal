# -*- coding: utf-8 -*-
"""SQLite 存储层：把 DataPacket 落进和老工程**完全兼容**的库里。

来源：老工程 com/db.py（peewee）。这里改用标准库 sqlite3：
  * 建表语句逐字照抄老库的 DDL（列名/列顺序/类型都一致），分析代码不用改
  * 单独一个写线程 + 队列，采集线程不被磁盘 IO 拖住
  * 会话目录沿用老习惯：实验结果/<用户名>_<任务>_<YYYYmmdd>/normal_result/database.db

各设备字段映射见 _write_packet()。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from core.A0_data_collect.base import DataPacket, now_str

log = logging.getLogger(__name__)

# 与老库逐字一致的建表语句（改这里等于改契约，务必同步 README）
DDL = [
    'CREATE TABLE IF NOT EXISTS "alert_act" ("id" INTEGER NOT NULL PRIMARY KEY, "events" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_ecg" ("id" INTEGER NOT NULL PRIMARY KEY, "Mac_device" TEXT NOT NULL, "value" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_emg" ("id" INTEGER NOT NULL PRIMARY KEY, "value" BLOB NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_gpu" ("id" INTEGER NOT NULL PRIMARY KEY, "value" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_grip_raw" ("id" INTEGER NOT NULL PRIMARY KEY, "side" TEXT NOT NULL, "device_time" DATETIME NOT NULL, "raw" TEXT NOT NULL, "battery" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_video_frame" ("id" INTEGER NOT NULL PRIMARY KEY, "camera" TEXT NOT NULL, "frame_index" INTEGER NOT NULL, "path" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_grip_l" ("id" INTEGER NOT NULL PRIMARY KEY, "time" DATETIME NOT NULL, "angle" TEXT NOT NULL, "press" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_grip_r" ("id" INTEGER NOT NULL PRIMARY KEY, "time" DATETIME NOT NULL, "angle" TEXT NOT NULL, "press" TEXT NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_serial" ("id" INTEGER NOT NULL PRIMARY KEY, "value" BLOB NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "data_udp" ("id" INTEGER NOT NULL PRIMARY KEY, "value" BLOB NOT NULL, "rec_time" BLOB NOT NULL, "created_date" DATETIME NOT NULL)',
    'CREATE TABLE IF NOT EXISTS "log" ("id" INTEGER NOT NULL PRIMARY KEY, "user_id" INTEGER NOT NULL, "type" TEXT NOT NULL, "message" TEXT NOT NULL, "created_date" DATETIME NOT NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id"))',
    'CREATE TABLE IF NOT EXISTS "user" ("id" INTEGER NOT NULL PRIMARY KEY, "username" VARCHAR(255) NOT NULL, "created_date" DATETIME NOT NULL)',
]

DEVICE_TABLE = {
    'ecg': 'data_ecg',
    'eye': 'data_udp',
    'emg': 'data_emg',
    'gpu': 'data_gpu',
    'alert': 'alert_act',
    'serial': 'data_serial',
}


def session_dirs(user: str, task: str, root: str = '实验结果', result_dir: str = 'normal_result',
                 db_name: str = 'database.db') -> Dict[str, str]:
    """按老工程规则算会话目录：同名前缀的会话目录存在就复用，否则新建当天的。

    返回 {'session_dir': 会话根, 'result_dir': 结果目录, 'db_path': 库文件}
    """
    os.makedirs(root, exist_ok=True)
    prefix = f'{user}_{task}'
    session_dir = None
    for item in sorted(os.listdir(root)):
        path = os.path.join(root, item)
        if os.path.isdir(path) and item.startswith(prefix):
            session_dir = path
            break
    if session_dir is None:
        session_dir = os.path.join(root, f'{prefix}_{datetime.now().strftime("%Y%m%d")}')
    out_dir = os.path.join(session_dir, result_dir)
    os.makedirs(out_dir, exist_ok=True)
    return {'session_dir': os.path.abspath(session_dir), 'result_dir': os.path.abspath(out_dir),
            'db_path': os.path.join(os.path.abspath(out_dir), db_name)}


def _db_writable(path: str) -> bool:
    """库文件已经存在时，探测当前身份能不能写。

    不存在就当可以（真建不出来会在 open() 里报出来）。
    """
    if not os.path.exists(path):
        return True
    try:
        with open(path, 'r+b'):
            pass
    except OSError:
        return False
    return True


def usable_session_dirs(user: str, task: str, root: str = '实验结果', result_dir: str = 'normal_result',
                        db_name: str = 'database.db', fresh: bool = False,
                        verbose: bool = True) -> Dict[str, str]:
    """给测试/自检用：要一份「现在就能写」的会话目录。

    session_dirs() 按老工程习惯复用同名旧会话目录，复用本身没问题；
    但复用的那个库可能是别的会话、别的账号建的（能读不能写），
    或者上一轮跑崩了留下 WAL —— 直接开库就是 readonly。
    这里先探测一下，不可用（或 fresh=True）就换一个带时间戳的会话名，
    保证同一条命令反复跑也是绿的。
    """
    dirs = session_dirs(user, task, root, result_dir, db_name)
    if fresh or not _db_writable(dirs['db_path']):
        reason = '调用方要求 fresh' if fresh else '已存在的库不可写'
        rotated_task = f'{task}_{datetime.now().strftime("%H%M%S")}'
        rotated = session_dirs(user, rotated_task, root, result_dir, db_name)
        if verbose:
            log.warning('会话库 %s（%s），改用 %s', dirs['db_path'], reason, rotated['db_path'])
        dirs = rotated
    return dirs


def usable_session_db_path(user: str, task: str, root: str = '实验结果', result_dir: str = 'normal_result',
                           db_name: str = 'database.db', fresh: bool = False) -> str:
    """usable_session_dirs() 的省事版：只要库文件路径。"""
    return usable_session_dirs(user, task, root, result_dir, db_name, fresh)['db_path']


class FileStore:
    """会话文件落盘：视频、帧图、分析结果图，路径规则统一走这里。"""

    def __init__(self, result_dir: str):
        self.result_dir = os.path.abspath(result_dir)
        os.makedirs(self.result_dir, exist_ok=True)

    def dir(self, *parts: str) -> str:
        path = os.path.join(self.result_dir, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def path(self, *parts: str) -> str:
        return os.path.join(self.result_dir, *parts)

    session_dirs = staticmethod(session_dirs)      # 方便 FileStore.session_dirs(...) 调用


class SqliteStore:
    """一个实验会话对应一个库；写线程独立于采集线程。"""

    def __init__(self, db_path: str, flush_interval: float = 1.0, batch_size: int = 200):
        self.db_path = db_path
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self._queue: 'queue.Queue[Optional[DataPacket]]' = queue.Queue(maxsize=100000)
        self._conn: Optional[sqlite3.Connection] = None
        self._writer: Optional[threading.Thread] = None
        self._running = False
        self.written = 0
        self.unknown_devices: set = set()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or '.', exist_ok=True)

    # ------------------------------------------------------------- 会话目录
    @staticmethod
    def session_db_path(user: str, task: str, root: str = '实验结果', result_dir: str = 'normal_result',
                        db_name: str = 'database.db') -> str:
        """沿用老工程的目录规则：同名前缀的会话目录存在就复用，否则新建当天的。"""
        return session_dirs(user, task, root, result_dir, db_name)['db_path']

    # ------------------------------------------------------------- 生命周期
    def open(self) -> 'SqliteStore':
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute('PRAGMA journal_mode=WAL')       # 采集时写入更稳
            conn.execute('PRAGMA synchronous=NORMAL')
            for ddl in DDL:
                conn.execute(ddl)
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            # 最常见的坑：库文件是别的会话/别的账号建的，当前身份写不进去，
            # SQLite 只报「attempt to write a readonly database」，看不出原因。
            raise RuntimeError(
                f'打开会话库失败：{self.db_path}（{exc}）。'
                f'若提示 readonly，说明这个库当前身份写不了（多是别的会话留下的），'
                f'换一个会话名（--task）或换目录，或把会话目录里的 database.db* 清掉再跑。'
            ) from exc
        self._conn = conn
        self._ensure_default_user()
        self.log(f'会话数据库就绪：{self.db_path}', user=None)
        return self

    def start(self) -> 'SqliteStore':
        if self._conn is None:
            self.open()
        self._running = True
        self._writer = threading.Thread(target=self._write_loop, name='store-writer', daemon=True)
        self._writer.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._running = False
        self._queue.put(None)
        if self._writer is not None:
            self._writer.join(timeout)
            self._writer = None
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None
        log.info('存储已关闭，共写入 %d 条', self.written)

    # 供 Manager.add_sink 使用
    def submit(self, packet: DataPacket) -> None:
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            log.warning('存储队列已满，丢弃 %s 的一包数据', packet.device)

    # ------------------------------------------------------------- 写入
    def _write_loop(self) -> None:
        pending = 0
        while True:
            try:
                packet = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                if self._conn is not None:
                    self._conn.commit()
                pending = 0
                if not self._running:
                    return
                continue
            if packet is None:
                break
            try:
                self._write_packet(packet)
                self.written += 1
                pending += 1
                if pending >= self.batch_size:
                    self._conn.commit()
                    pending = 0
            except Exception:
                log.exception('写入 %s 数据失败', packet.device)
        if self._conn is not None:
            self._conn.commit()

    def _write_packet(self, packet: DataPacket) -> None:
        conn = self._conn
        if conn is None:
            return
        device = packet.device
        created = packet.t_recv or now_str()

        if device == 'ecg':
            # value 必须是 JSON（分析端 json.loads(row[2])['ECG']）
            conn.execute('INSERT INTO data_ecg ("Mac_device", "value", "created_date") VALUES (?,?,?)',
                         (str(packet.meta.get('mac', '')), json.dumps(packet.payload, ensure_ascii=False), created))
        elif device == 'eye':
            value = json.dumps(packet.payload, ensure_ascii=False).encode('utf-8')
            conn.execute('INSERT INTO data_udp ("value", "rec_time", "created_date") VALUES (?,?,?)',
                         (value, created, created))
        elif device == 'emg':
            value = json.dumps(packet.payload, ensure_ascii=False).encode('utf-8')
            conn.execute('INSERT INTO data_emg ("value", "created_date") VALUES (?,?)', (value, created))
        elif device == 'grip':
            # 方案二：A0 只写原始帧，加工交给 A2_analyze_grip
            conn.execute('INSERT INTO data_grip_raw ("side", "device_time", "raw", "battery", "created_date")'
                         ' VALUES (?,?,?,?,?)',
                         (str(packet.meta.get('side') or packet.payload.get('side', 'L')),
                          packet.payload.get('time', created),
                          json.dumps(packet.payload.get('raw', []), ensure_ascii=False),
                          str(packet.payload.get('battery', '')),
                          created))
        elif device == 'alert':
            conn.execute('INSERT INTO alert_act ("events", "created_date") VALUES (?,?)',
                         (json.dumps(packet.payload, ensure_ascii=False), created))
        elif device == 'video_frame':
            conn.execute('INSERT INTO data_video_frame ("camera", "frame_index", "path", "created_date")'
                         ' VALUES (?,?,?,?)',
                         (str(packet.payload.get('camera', '')),
                          int(packet.payload.get('index', 0)),
                          str(packet.payload.get('path', '')),
                          created))
        elif device == 'gpu':
            conn.execute('INSERT INTO data_gpu ("value", "created_date") VALUES (?,?)',
                         (json.dumps(packet.payload, ensure_ascii=False), created))
        elif device == 'serial':
            value = json.dumps(packet.payload, ensure_ascii=False).encode('utf-8')
            conn.execute('INSERT INTO data_serial ("value", "created_date") VALUES (?,?)', (value, created))
        else:
            if device not in self.unknown_devices:
                self.unknown_devices.add(device)
                log.warning('没有对应表，忽略设备：%s', device)

    # ------------------------------------------------------------- 用户/日志
    def _ensure_default_user(self) -> None:
        conn = self._conn
        if conn is None:
            return
        row = conn.execute('SELECT id FROM "user" WHERE "username" = ?', ('admin',)).fetchone()
        if row is None:
            conn.execute('INSERT INTO "user" ("username", "created_date") VALUES (?,?)', ('admin', now_str()))
            conn.commit()

    def log(self, message: str, user: Optional[str] = 'admin', type_: str = '系统') -> None:
        """写日志表。老库 user_id 列里存的是用户名字符串，这里保持一致。"""
        conn = self._conn
        if conn is None:
            return
        conn.execute('INSERT INTO "log" ("user_id", "type", "message", "created_date") VALUES (?,?,?,?)',
                     (user or '', type_, message, now_str()))
        conn.commit()

    # ------------------------------------------------------------- 查询（测试/自检用）
    def counts(self) -> Dict[str, int]:
        conn = sqlite3.connect(self.db_path)
        try:
            out = {}
            for table in ('data_ecg', 'data_udp', 'data_emg', 'data_grip_raw', 'data_grip_l', 'data_grip_r',
                  'data_video_frame', 'alert_act', 'data_gpu'):
                try:
                    out[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                except sqlite3.Error:
                    out[table] = -1
            return out
        finally:
            conn.close()


def insert_legacy_grip_rows(conn: sqlite3.Connection, side: str, rows) -> int:
    """A2 加工完成后调用：写老格式 data_grip_l / data_grip_r（保持分析兼容）。"""
    table = 'data_grip_r' if str(side).upper().startswith('R') else 'data_grip_l'
    conn.executemany(f'INSERT INTO {table} ("time", "angle", "press", "created_date")'
                     ' VALUES (?,?,?,?)', rows)
    return len(rows)
