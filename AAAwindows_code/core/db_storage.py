#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库存储模块（与接收程序解耦，可单独使用）。

功能：
  - 在 `<项目根>/data/` 下按当前系统时间创建 SQLite 数据库文件，例如 data/20260903_183000.db
  - 数据按类型写入不同的数据表：ecg_data / breath_data / triaxial_data / temperature_data 等
  - 每张表包含两列：
        time   当前系统时间（文本）
        value  传感器数据（标量或复杂结构，统一以 JSON 字符串存储）

其他传感器接入时，只需调用 SensorDB.insert / insert_many 即可，无需改动本文件。

注意：`DATA_DIR` 由 `core.config` 按「本文件所在目录」推导为绝对路径，
不再依赖启动时的当前工作目录（改造前从别的目录启动，数据会散落到别处）。
"""

import json
import os
import sqlite3
import time
from datetime import datetime

from core.config import DATA_DIR

# 手环（绿光手环）数据表名：与心电表同库不同表，统一由 DataRecorder.record_bracelet 分流
BRACELET_PPG_TABLE = "bracelet_ppg_data"          # 4 通道 PPG，每个采样点一行 {"PPG1":..,"PPG2":..,"PPG3":..,"PPG4":..}
BRACELET_ACCEL_TABLE = "bracelet_accel_data"      # 腕部三轴，每包一行 {"x":..,"y":..,"z":..}
BRACELET_GSR_TABLE = "bracelet_gsr_data"          # 皮电，每个样本一行（电导值）
BRACELET_SKIN_TEMP_TABLE = "bracelet_skin_temp"   # 肤温，每次上报一行（标量，℃）
BRACELET_BATTERY_TABLE = "bracelet_battery"       # 电量，每次上报一行 {"adc":..,"percent":..}


# 批量提交默认参数（避免每次 insert 都 fsync，减少对事件循环/写线程的阻塞）
DEFAULT_COMMIT_INTERVAL = 0.5   # 秒：距上次提交超过该时间就提交一次
DEFAULT_COMMIT_ROWS = 500       # 行：累计新增行数达到该值就提交一次


def now_str():
    """返回当前系统时间字符串，作为数据的写入时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


class SensorDB:
    """简单的传感器数据 SQLite 存储。

    表结构统一为 (time TEXT, value TEXT)，value 对复杂结构做 JSON 序列化，
    因此不同传感器无需预先定义表结构，写入时自动建表。
    """

    def __init__(self, data_dir=DATA_DIR, db_name=None,
                 commit_interval=DEFAULT_COMMIT_INTERVAL,
                 commit_rows=DEFAULT_COMMIT_ROWS,
                 session_subdir=None):
        """`session_subdir` 用于「组别/人物名_实验时间」这种分层存放，
        例如 session_subdir='A组/张三_20261024_160000' 会建到
        `data/A组/张三_20261024_160000/<时间戳>.db`。"""
        self.data_dir = data_dir
        if session_subdir:
            data_dir = os.path.join(data_dir, *str(session_subdir).replace('\\', '/').split('/'))
        os.makedirs(data_dir, exist_ok=True)

        if db_name is None:
            db_name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".db"
        self.db_path = os.path.join(data_dir, db_name)

        # 若同名文件已存在（如同一秒内重复启动），自动追加序号，避免覆盖旧数据
        base, ext = os.path.splitext(self.db_path)
        counter = 1
        while os.path.exists(self.db_path):
            self.db_path = "{}_{:02d}{}".format(base, counter, ext)
            counter += 1

        # 批量提交参数：每累计 commit_rows 行、或距上次提交超过 commit_interval 秒才 commit
        self.commit_interval = commit_interval
        self.commit_rows = commit_rows
        self._pending_rows = 0
        self._last_commit = time.time()

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        # WAL 下 synchronous=NORMAL 已可保证进程崩溃不损坏数据库，且 fsync 次数大幅减少
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")   # 摄像头帧写入同一个库时避免短暂 locked

    def _ensure_table(self, table):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS {} (time TEXT, value TEXT)".format(table)
        )

    @staticmethod
    def _encode(value):
        # 字符串原样存储，其余（int/float/dict/list）统一 JSON 序列化
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def _maybe_commit(self):
        """按行数/时间阈值提交事务，避免每条记录一次 fsync。"""
        if self._pending_rows < self.commit_rows:
            if time.time() - self._last_commit < self.commit_interval:
                return
        self.commit()

    def commit(self):
        """显式提交当前事务。"""
        if self._pending_rows == 0:
            self._last_commit = time.time()
            return
        self.conn.commit()
        self._pending_rows = 0
        self._last_commit = time.time()

    def insert(self, table, value, ts=None):
        """写入单条记录。value 为标量或 dict/list。"""
        ts = ts or now_str()
        self._ensure_table(table)
        self.conn.execute(
            "INSERT INTO {} (time, value) VALUES (?, ?)".format(table),
            (ts, self._encode(value)),
        )
        self._pending_rows += 1
        self._maybe_commit()

    def insert_many(self, table, values, ts=None):
        """批量写入。values 为标量/dict 列表，共享同一时间戳。"""
        if not values:
            return
        ts = ts or now_str()
        self._ensure_table(table)
        rows = [(ts, self._encode(v)) for v in values]
        self.conn.executemany(
            "INSERT INTO {} (time, value) VALUES (?, ?)".format(table), rows
        )
        self._pending_rows += len(rows)
        self._maybe_commit()

    def close(self):
        """提交并关闭数据库连接。"""
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


class DataRecorder:
    """统一的传感器数据入库接口（供各接收模块调用）。

    当前已接入：心电(ecg_data)、体温(temperature_data)、呼吸(breath_data)、
    三轴(triaxial_data)。

    接入其他传感器的方式（二选一）：
      1. 直接调用通用接口：recorder.store("spo2_data", [98, 97, ...])
      2. 在下方新增 store_xxx 方法，内部复用 self.store / self.storage
    """

    def __init__(self, storage: SensorDB):
        self.storage = storage

    def store(self, table, values, ts=None):
        """通用写入接口：values 为标量或标量/dict 列表。"""
        if isinstance(values, (list, tuple)):
            self.storage.insert_many(table, list(values), ts)
        else:
            self.storage.insert(table, values, ts)

    def flush(self):
        """提交当前未提交的数据（由写库线程周期性调用）。"""
        self.storage.commit()

    def record(self, decoded, ts=None):
        """按解码结果中的类型字段，分别写入对应数据表。"""
        ts = ts or now_str()

        ecg = decoded.get("ECG")
        if ecg:
            if "体温" in ecg:
                self.storage.insert("temperature_data", ecg["体温"], ts)
            if "ECG" in ecg:
                self.storage.insert_many("ecg_data", ecg["ECG"], ts)

        if "呼吸" in decoded:
            self.storage.insert_many("breath_data", decoded["呼吸"], ts)

        if "三轴" in decoded:
            self.storage.insert_many("triaxial_data", decoded["三轴"], ts)

    # ------------------------------------------------------------------
    # 手环（绿光手环）数据分流
    # ------------------------------------------------------------------
    def record_bracelet(self, decoded, ts=None):
        """按手环解码结果中的类型字段，分别写入对应数据表。

        decoded 结构（由 bracelet_receiver 中的回调产出，每个数据包一个时间戳）：
            {
                "PPG": [{"PPG1":..,"PPG2":..,"PPG3":..,"PPG4":..}, ...],  # 整包 10 个采样点
                "腕部三轴": {"x":..,"y":..,"z":..},
                "GSR": [..],                # 10 个皮肤电导值
                "肤温": 36.5,               # 摄氏度
                "电量": {"adc":.., "percent":..},
            }
        """
        ts = ts or now_str()

        if decoded.get("PPG"):
            self.store(BRACELET_PPG_TABLE, decoded["PPG"], ts)

        if "腕部三轴" in decoded:
            self.store(BRACELET_ACCEL_TABLE, decoded["腕部三轴"], ts)

        if decoded.get("GSR"):
            self.store(BRACELET_GSR_TABLE, decoded["GSR"], ts)

        if "肤温" in decoded:
            self.store(BRACELET_SKIN_TEMP_TABLE, decoded["肤温"], ts)

        if decoded.get("电量"):
            self.store(BRACELET_BATTERY_TABLE, decoded["电量"], ts)

    # ------------------------------------------------------------------
    # 其他传感器接入点（示例，按需取消注释并实现）：
    #
    # def store_spo2(self, spo2, ts=None):
    #     self.storage.insert("spo2_data", spo2, ts)
    # ------------------------------------------------------------------
