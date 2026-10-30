# -*- coding: utf-8 -*-
"""眼动窗口特征（A2）：data_udp -> 每个时间窗的 pupil / blink_rate。

来源：老工程 load_analyze/ecg_process.py 的 get_eye_feature + eye_sliding_window
+ caculate_sample_rate（负荷模型的 10 维特征里，眼动只占 pupil、blink_rate 两维，
就是这里出来的）。流程逐字照搬：

1. 读 data_udp：``value = json.loads(row[1])``（BLOB，SmartEye 解码字典）
2. 取 `PupilDiameter` 和 `Blink` 两个字段，时间取 row[3]（created_date）
3. 时间轴同样是 ``pd.date_range(首, 末, periods=样本数)`` 均匀铺开
4. 采样率 = int(样本数 / 首末时间差秒数)（老代码 caculate_sample_rate）
5. 滑窗取均值：窗内 pupil 均值、窗内 Blink 均值（即 blink_rate）

注意与 `eye_analyze/eye_analyze.py` 的区别（老工程里是两套东西）：

* `eye_analyze.py` 吃的是 **Tobii** 数据（`Data_tobii` 表里的
  left/right_gaze_point_on_display_area、*_eye_openness_value），算的是
  注视时长、扫视速度、瞳孔直径、眨眼频率/时长，配套眼动分析界面画曲线。
* 新工程的 A0 眼动采集器解的是 **SmartEye** 报文（FrameNumber / PupilDiameter /
  Blink / Saccade / Fixation / EyelidOpening …），没有 Tobii 那些字段，
  所以 `eye_analyze.py` 那套注视/扫视聚类**没法照搬**（照搬只会得到空表）。
  真要补注视/扫视口径，得先定用哪些 SmartEye 字段、用什么阈值——见 README「待定」。
"""
from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

log = logging.getLogger(__name__)

# 老代码 eye_sliding_window 里用到的两个字段名（SmartEye 解码字典的键）
PAYLOAD_PUPIL_KEY = 'PupilDiameter'
PAYLOAD_BLINK_KEY = 'Blink'

DEFAULT_CONFIG: Dict[str, Any] = {
    'window_size': 60.0,         # 老代码 windows_size=60（秒）
    'overlap': 0.8,              # 老代码 overlap_rate=0.8
}

DB_TABLE = 'data_udp'
DB_TIME_COLUMN = 'created_date'


def _to_time_str(value: Any) -> str:
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S.%f')
    return str(value)


class EyeProcessor:
    """眼动窗口特征加工器。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.window_size: float = float(cfg['window_size'])
        self.overlap: float = float(cfg['overlap'])
        self.last_window_time: List[Any] = []
        self.last_sample_rate: int = 0

    # ------------------------------------------------------------- 读库
    @staticmethod
    def read_rows(db_path: str, start: Any = None, end: Any = None) -> List[tuple]:
        conn = sqlite3.connect(str(db_path))
        try:
            if start is None or end is None:
                first, last = conn.execute(
                    f'SELECT MIN({DB_TIME_COLUMN}), MAX({DB_TIME_COLUMN}) FROM {DB_TABLE}').fetchone()
                if first is None:
                    raise RuntimeError(f'{db_path} 的 {DB_TABLE} 表里没有数据')
                start = start if start is not None else first
                end = end if end is not None else last
            cursor = conn.execute(
                f'SELECT * FROM {DB_TABLE} WHERE {DB_TIME_COLUMN} BETWEEN ? AND ?',
                (_to_time_str(start), _to_time_str(end)))
            return cursor.fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------- 工具
    @staticmethod
    def sample_rate(start: Any, end: Any, length: int) -> int:
        """老代码 caculate_sample_rate：样本数 / 时长，取整。"""
        total_seconds = (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds()
        if total_seconds <= 0:
            return 0
        return int(length / total_seconds)

    def sliding_window(self, pupil: Sequence[float], blink: Sequence[float],
                       time: Sequence[Any], sample_rate: int) -> Tuple[List[float], List[float], List[Any]]:
        """窗内取均值（老代码 eye_sliding_window 逐行照搬）。"""
        window_samples = int(self.window_size * sample_rate)
        overlap_samples = int(window_samples * self.overlap)

        pupils: List[float] = []
        blinks: List[float] = []
        times: List[Any] = []
        start_index = 0
        while start_index + window_samples <= len(pupil):
            pupils.append(pd.Series(pupil[start_index:start_index + window_samples]).mean())
            blinks.append(pd.Series(blink[start_index:start_index + window_samples]).mean())
            window_time = time[start_index:start_index + window_samples]
            times.append(window_time[window_samples // 2])
            start_index += window_samples - overlap_samples
        return pupils, blinks, times

    # ------------------------------------------------------------- 主流程
    def process(self, pupil: Sequence[float], blink: Sequence[float],
                timestamps: Sequence[Any]) -> pd.DataFrame:
        time_axis = pd.date_range(start=timestamps[0], end=timestamps[-1], periods=len(pupil))
        sample_rate = self.sample_rate(time_axis[0], time_axis[-1], len(pupil))
        self.last_sample_rate = sample_rate
        if sample_rate <= 0:
            log.warning('[eye] 采样率算出来是 %s，无法切窗', sample_rate)
            self.last_window_time = []
            return pd.DataFrame({'pupil': [], 'blink_rate': []})

        pupils, blinks, times = self.sliding_window(pupil, blink, time_axis, sample_rate)
        self.last_window_time = list(times)
        # 老代码就这两列，时间列不加（时间轴由心电特征那张表提供）
        return pd.DataFrame({'pupil': pupils, 'blink_rate': blinks})

    def analyze(self, db_path: str, start: Any = None, end: Any = None) -> pd.DataFrame:
        rows = self.read_rows(db_path, start, end)
        if not rows:
            raise RuntimeError(f'{db_path} 在 {start} ~ {end} 之间没有眼动数据')

        pupil: List[float] = []
        blink: List[float] = []
        timestamps: List[Any] = []
        for row in rows:
            value = json.loads(row[1])
            pupil.append(value.get(PAYLOAD_PUPIL_KEY, 0))
            blink.append(value.get(PAYLOAD_BLINK_KEY, 0))
            timestamps.append(row[3])
        if len(pupil) < 2:
            raise RuntimeError(f'{db_path} 的眼动数据只有 {len(pupil)} 条，切不了窗')
        return self.process(pupil, blink, timestamps)


def compute_eye_features(db_path: str, start: Any = None, end: Any = None,
                         config: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """便捷入口：等价于老代码 `get_eye_feature(db_path, start, end)`。"""
    return EyeProcessor(config).analyze(db_path, start, end)
