# -*- coding: utf-8 -*-
"""心电特征（A2）：data_ecg -> HRV 特征表（每个时间窗一行）。

来源：老工程 load_analyze/ecg_process.py 的 Ecg_Compute.get_feature。
（load_analyze 那份才是界面在用的：ecg_analyze_window.py / load_analyze_window.py 都是
`from load_analyze.ecg_process import get_input`；根目录的 ecg_process.py 是零引用重复件。）

流程逐字照搬：

1. 读 data_ecg：``value = json.loads(row[2])['ECG']``，时间取 row[3]（created_date）
2. 小波去噪：db6 / level 9
3. 滑窗：``window_samples = int(window_size * sample_rate)``，
   步长 = 窗口 - int(窗口 * overlap)
4. R 峰：biosppy `hamilton_segmenter` + `correct_rpeaks`
5. RR 间期：1000*diff(rpeaks)/sr -> remove_outliers(300,2000) -> 线性插值
   -> malik 去异位搏动 -> 再线性插值
6. 特征：bpm / sdnn / rmssd / vlf / lf / hf / lf-hf 比 / sd1 / sd2 / sd2-sd1 比
7. 返回前删掉 sdnn、sd2（老代码就是这么删的），再加 timestamp 列（窗口中点时间）

**两个「老代码的怪癖」原样保留**——负荷模型就是按这个口径训出来的，改了等于换口径：

* 小波软阈值那一行算出 threshold 之后**没有写回** cD4~cD9（老代码 `threshold = pywt.threshold(...)`
  之后只有 `threshold = np.array(threshold)`），所以细节层其实没被阈值化；这里照搬。
* 时间轴用 ``pd.date_range(start=首条时间, end=末条时间, periods=样本数)`` 均匀铺开，
  不是真实采样时刻；这里照搬。

老代码的 `ecg_queue`（把特征塞给 predict_process 线程）不要了：
新版由 A2 pipeline 直接返回 DataFrame，界面/推理链路自己订阅。
"""
from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pywt
from biosppy.signals import ecg as biosppy_ecg
from hrvanalysis import (get_frequency_domain_features, get_poincare_plot_features,  # noqa: F401
                         get_time_domain_features, interpolate_nan_values, remove_ectopic_beats,
                         remove_outliers)

log = logging.getLogger(__name__)

WAVELET = 'db6'
WAVELET_LEVEL = 9

# 一个窗口里至少要这么多心搏才算得出 HRV 特征。
# 少于这个数时 hrvanalysis 的 interpolate_nan_values 会直接数组越界（老代码在这里就是崩的）
MIN_BEATS = 4

# 老代码算出来的全部列（顺序一致）；返回前会删 drop_columns
FEATURE_COLUMNS = ['bpm', 'sdnn', 'rmssd', 'vlf', 'lf', 'hf', 'lf/hf', 'sd1', 'sd2', 'sd2/sd1']
DEFAULT_DROP_COLUMNS = ['sdnn', 'sd2']

DEFAULT_CONFIG: Dict[str, Any] = {
    'sample_rate': 240,          # 老代码 get_feature 的 Sample_rate 默认值
    'window_size': 60.0,         # 老代码 windows_size=60（秒）
    'overlap': 0.8,              # 老代码 overlap_rate=0.8
    'drop_columns': DEFAULT_DROP_COLUMNS,
}

DB_TABLE = 'data_ecg'
DB_TIME_COLUMN = 'created_date'


def _to_time_str(value: Any) -> str:
    """datetime -> 老库的 created_date 文本格式（字符串原样返回）。"""
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S.%f')
    return str(value)


class EcgProcessor:
    """心电特征加工器：波形进、DataFrame 出。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.sample_rate: int = int(cfg['sample_rate'])
        self.window_size: float = float(cfg['window_size'])
        self.overlap: float = float(cfg['overlap'])
        self.drop_columns: Tuple[str, ...] = tuple(cfg.get('drop_columns') or ())
        self.last_window_time: List[Any] = []          # 最近一次各窗口的中点时间（界面画图用）

    # ------------------------------------------------------------- 读库
    @staticmethod
    def read_rows(db_path: str, start: Any = None, end: Any = None) -> List[tuple]:
        """按时间范围取 data_ecg 的原始行（SELECT *，分析端就是靠列序取值）。"""
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

    # ------------------------------------------------------------- 波形处理
    @staticmethod
    def wavelet_denoise(signal: Sequence[float]) -> np.ndarray:
        """小波去噪（老代码 _wavelet_denoise 逐行照搬，含没写回阈值的怪癖）。"""
        coeffs = pywt.wavedec(signal, WAVELET, level=WAVELET_LEVEL)
        cA9, cD9, cD8, cD7, cD6, cD5, cD4, cD3, cD2, cD1 = [np.array(coeff) for coeff in coeffs]

        # 丢掉最低频（基线）和最高频三层
        cA9[:] = 0
        cD1[:] = 0
        cD2[:] = 0
        cD3[:] = 0

        # 老代码在这里算了软阈值但没赋回系数，这里保持原样（改了就不是同一个口径了）
        for detail_coeff in [cD4, cD5, cD6, cD7, cD8, cD9]:
            pywt.threshold(detail_coeff, np.sqrt(2 * np.log(len(signal))), mode='soft')

        new_coeffs = [cA9, cD9, cD8, cD7, cD6, cD5, cD4, cD3, cD2, cD1]
        return pywt.waverec(new_coeffs, WAVELET)

    def sliding_window(self, value: Sequence[float],
                       time: Sequence[Any]) -> Tuple[List[np.ndarray], List[Any]]:
        """按采样率切窗；每个窗口只返回「中点时间」（老代码的 _sliding_window）。"""
        window_samples = int(self.window_size * self.sample_rate)
        overlap_samples = int(window_samples * self.overlap)

        windows_value: List[np.ndarray] = []
        windows_time: List[Any] = []
        start_index = 0
        while start_index + window_samples <= len(value):
            windows_value.append(value[start_index:start_index + window_samples])
            window_time = time[start_index:start_index + window_samples]
            windows_time.append(window_time[window_samples // 2])
            start_index += window_samples - overlap_samples
        return windows_value, windows_time

    @staticmethod
    def r_peaks(signal: Sequence[float], sample_rate: int) -> np.ndarray:
        """R 波位置：hamilton 分割 + 修正（老代码 _get_r_peaks）。"""
        rpeaks_raw = biosppy_ecg.hamilton_segmenter(signal, sample_rate)
        corrected = biosppy_ecg.correct_rpeaks(signal, rpeaks_raw[0], sample_rate)
        return corrected[0]

    @staticmethod
    def rr_intervals(r_peaks: Sequence[int], sample_rate: int) -> np.ndarray:
        """RR 间期清洗链（老代码 _get_rr_intervals 的步骤，但加了兜底）。

        老代码这三步（remove_outliers -> interpolate -> malik -> interpolate）在数据不理想时
        （心搏太少、开头就是 NaN）会让 hrvanalysis 抛 IndexError，整个分析直接中断。
        这里碰到这种情况返回空数组，交给 features() 把这个窗口跳过并告警——
        正常数据上的结果与老代码逐值一致（回归测试盯着）。
        """
        rr_intervals_list = 1000 * (np.diff(r_peaks) / sample_rate)
        if len(rr_intervals_list) < MIN_BEATS:
            return np.array([])
        try:
            without_outliers = remove_outliers(rr_intervals=rr_intervals_list, low_rri=300,
                                               high_rri=2000)
            if len(without_outliers) < MIN_BEATS:
                return np.array([])
            interpolated = interpolate_nan_values(rr_intervals=without_outliers,
                                                  interpolation_method='linear')
            nn_intervals = remove_ectopic_beats(rr_intervals=interpolated, method='malik')
            cleaned = interpolate_nan_values(rr_intervals=nn_intervals)
        except Exception as exc:                      # noqa: BLE001 - hrvanalysis 对短序列不友好
            log.warning('[ecg] RR 间期清洗失败（%s），本窗口跳过', exc)
            return np.array([])
        return np.asarray(cleaned) if len(cleaned) >= MIN_BEATS else np.array([])

    @staticmethod
    def append_features(feature_dic: Dict[str, List[float]], rr: Sequence[float]) -> bool:
        """算一个窗口的 HRV 特征并追加到 feature_dic（老口径）；算不出来返回 False。"""
        try:
            beats = 60000 / np.array(rr)
            time_domain = get_time_domain_features(rr)
            frequency_domain = get_frequency_domain_features(rr)
            poincare = get_poincare_plot_features(rr)
            values = {
                'bpm': float(np.mean(beats)),
                'sdnn': time_domain['sdnn'],
                'rmssd': time_domain['rmssd'],
                'vlf': frequency_domain['vlf'],
                'lf': frequency_domain['lf'],
                'hf': frequency_domain['hf'],
                'lf/hf': frequency_domain['lf_hf_ratio'],
                'sd1': poincare['sd1'],
                'sd2': poincare['sd2'],
                'sd2/sd1': poincare['ratio_sd2_sd1'],
            }
        except Exception as exc:                      # noqa: BLE001 - 单窗口失败不拖垮整段分析
            log.warning('[ecg] 该窗口 HRV 特征算不出来（%s），跳过', exc)
            return False
        for name, value in values.items():
            feature_dic[name].append(value)
        return True

    @classmethod
    def features(cls, rr_intervals_list: Sequence[Sequence[float]]) -> Dict[str, List[float]]:
        """一批窗口的 HRV 特征（心搏太少的窗口跳过，不抛异常）。"""
        feature_dic: Dict[str, List[float]] = {name: [] for name in FEATURE_COLUMNS}
        skipped = 0
        for rr in rr_intervals_list:
            if rr is None or len(rr) < MIN_BEATS:
                skipped += 1
                continue
            cls.append_features(feature_dic, rr)
        if skipped:
            log.warning('[ecg] %d 个窗口心搏不足 %d 次，已跳过（老代码在这里会抛 IndexError）',
                        skipped, MIN_BEATS)
        return feature_dic

    # ------------------------------------------------------------- 主流程
    def process(self, values: Sequence[float], timestamps: Sequence[Any]) -> pd.DataFrame:
        """波形 + 时间 -> 特征表（老代码 get_feature 去掉读库那一段）。"""
        time_axis = pd.date_range(start=timestamps[0], end=timestamps[-1], periods=len(values))
        filtered = self.wavelet_denoise(values)
        windows, window_time = self.sliding_window(filtered, time_axis)
        self.last_window_time = list(window_time)

        r_peaks_list = [self.r_peaks(window, self.sample_rate) for window in windows]
        rr_list = [self.rr_intervals(peaks, self.sample_rate) for peaks in r_peaks_list]

        # 逐窗口算特征：坏窗口（心搏不足）连它那行时间一起跳过，
        # 否则特征列和时间列长度对不上，DataFrame 直接报错
        feature_dic: Dict[str, List[float]] = {name: [] for name in FEATURE_COLUMNS}
        kept_time: List[Any] = []
        skipped = 0
        for index, rr in enumerate(rr_list):
            if rr is None or len(rr) < MIN_BEATS:
                skipped += 1
                continue
            if self.append_features(feature_dic, rr):
                kept_time.append(window_time[index])
            else:
                skipped += 1
        if skipped:
            log.warning('[ecg] %d/%d 个窗口不可用已跳过（老代码在这里会抛 IndexError）',
                        skipped, len(rr_list))

        feature_dic['timestamp'] = kept_time
        for column in self.drop_columns:
            feature_dic.pop(column, None)
        return pd.DataFrame(feature_dic)

    def analyze(self, db_path: str, start: Any = None, end: Any = None) -> pd.DataFrame:
        """读库 -> 拼波形 -> 出特征表。"""
        rows = self.read_rows(db_path, start, end)
        if not rows:
            raise RuntimeError(f'{db_path} 在 {start} ~ {end} 之间没有心电数据')

        values: List[float] = []
        timestamps: List[Any] = []
        for row in rows:
            values.extend(json.loads(row[2])['ECG'])
            timestamps.append(row[3])
        if len(values) < int(self.window_size * self.sample_rate):
            log.warning('[ecg] 样本数 %d 不足一个窗口（需要 %d），特征表会是空的',
                        len(values), int(self.window_size * self.sample_rate))
        return self.process(values, timestamps)


def compute_ecg_features(db_path: str, start: Any = None, end: Any = None,
                         config: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """便捷入口：等价于老代码 `Ecg_Compute().get_feature(db_path, start, end)`。"""
    return EcgProcessor(config).analyze(db_path, start, end)
