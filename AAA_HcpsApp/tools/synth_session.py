# -*- coding: utf-8 -*-
"""造一个「合成会话库」，给 A2 分析层的测试和回归用（不需要硬件，秒级跑完）。

为什么不直接跑采集器：分析层要的是几十秒到几分钟的数据，而采集器按实时速率
产生数据，跑一次就得等同样久。这里按**老库的写入契约**（A1 SqliteStore）一次性灌数据，
分析代码完全不知道数据是造的还是采的：

* data_ecg.value = json.dumps({'ECG': [...], 'send_time': '...'})，240 Hz
* data_udp.value = json.dumps(SmartEye 字典 + send_time).encode()，20 Hz
* created_date  = '%Y-%m-%d %H:%M:%S.%f'

心电用常见的 PQRST 高斯合成（R 峰明显，biosppy 的 hamilton 分割器能稳定识别），
眼动用正弦瞳孔直径 + 周期性眨眼，形态跟 SimCollector 一致。
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A0_data_collect.base import DataPacket        # noqa: E402
from core.A1_data_store import SqliteStore, usable_session_dirs   # noqa: E402

TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
DEFAULT_START = datetime(2026, 9, 15, 10, 0, 0)


def _gauss(t: np.ndarray, mu: float, sigma: float) -> np.ndarray:
    return np.exp(-((t - mu) ** 2) / (2 * sigma ** 2))


def synth_ecg(seconds: float, sample_rate: int = 240, bpm: float = 60.0,
              seed: int = 7, hrv: float = 0.05) -> List[float]:
    """合成一段带 P/Q/R/S/T 的心电。

    心搏不是等间距的——按呼吸性窦性心律不齐（0.25Hz，幅度 hrv）叠一点随机抖动来摆 R 峰。
    这一步很关键：等间距心电算出来的 rmssd / hf / lf÷hf 全是 0 或者 0/0=NaN，
    SVM 这类不支持缺失值的模型会直接报错，看着像分析层坏了，其实是数据太假。
    """
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    signal = np.zeros(n)

    rng = np.random.RandomState(seed)
    beats = []
    beat = 0.5
    while beat < seconds:
        beats.append(beat)
        rr = 60.0 / bpm * (1.0 + hrv * math.sin(2 * math.pi * 0.25 * beat) + rng.normal(0, 0.01))
        beat += max(rr, 0.4)

    for beat in beats:
        signal += 0.12 * _gauss(t, beat - 0.20, 0.020)      # P
        signal += -0.15 * _gauss(t, beat - 0.025, 0.008)    # Q
        signal += 1.00 * _gauss(t, beat, 0.010)             # R
        signal += -0.25 * _gauss(t, beat + 0.030, 0.010)    # S
        signal += 0.30 * _gauss(t, beat + 0.220, 0.050)     # T
    signal += rng.normal(0, 0.01, n)
    return [round(float(v), 6) for v in signal]


def synth_eye_payload(index: int, rate_hz: float = 20.0) -> Dict[str, Any]:
    """一帧 SmartEye 风格的眼动数据（字段名与 A0 eye 解码器一致）。"""
    return {
        'FrameNumber': index,
        'TimeStamp': int(1e6 * index / rate_hz),
        'FrameRate': float(rate_hz),
        'PupilDiameter': round(3.2 + 0.4 * math.sin(index / 25.0), 4),
        'Blink': 1 if index % 120 in (0, 1) else 0,
        'Saccade': 1 if index % 47 == 0 else 0,
        'Fixation': 1 if index % 47 != 0 else 0,
    }


def build_session(db_path: Optional[str] = None, seconds: float = 120.0,
                  sample_rate: int = 240, eye_rate: float = 20.0, bpm: float = 60.0,
                  start: Optional[datetime] = None, user: str = 'smoke',
                  task: str = 'analyze', root: Optional[str] = None,
                  fresh: bool = False) -> Dict[str, Any]:
    """写一个会话库，返回 {'db_path', 'start', 'end', 'ecg_packets', 'eye_packets'}。"""
    if db_path is None:
        dirs = usable_session_dirs(user=user, task=task,
                                   root=root or str(ROOT / '实验结果'), fresh=fresh)
        db_path = dirs['db_path']
    start = start or DEFAULT_START

    ecg = synth_ecg(seconds, sample_rate, bpm)
    samples_per_packet = 80                                  # 与 SimCollector 一致
    packet_seconds = samples_per_packet / sample_rate

    store = SqliteStore(db_path).start()
    for i in range(0, len(ecg), samples_per_packet):
        chunk = ecg[i:i + samples_per_packet]
        if len(chunk) < samples_per_packet:
            break
        created = (start + timedelta(seconds=i / sample_rate)).strftime(TIME_FORMAT)
        store.submit(DataPacket(device='ecg',
                                payload={'ECG': chunk, 'send_time': created},
                                t_recv=created, meta={'mac': 'AA:BB:CC:DD:EE:FF'}))

    eye_count = int(seconds * eye_rate)
    for i in range(eye_count):
        created = (start + timedelta(seconds=i / eye_rate)).strftime(TIME_FORMAT)
        payload = synth_eye_payload(i, eye_rate)
        payload['send_time'] = created
        store.submit(DataPacket(device='eye', payload=payload, t_recv=created))

    store.stop()
    return {
        'db_path': db_path,
        'start': start.strftime(TIME_FORMAT),
        'end': (start + timedelta(seconds=seconds)).strftime(TIME_FORMAT),
        'ecg_packets': len(ecg) // samples_per_packet,
        'eye_packets': eye_count,
        'packet_seconds': packet_seconds,
    }


if __name__ == '__main__':
    info = build_session(seconds=120, fresh=True)
    print(info)
