# -*- coding: utf-8 -*-
"""模拟数据源：没有硬件时也能开发界面、跑通链路、做回归测试。

替代老工程 test.py 里那套「往全局队列灌假数据」的写法。
产生两种包，字段结构与真实采集器保持一致：
    kind='ecg'  -> {'ECG': [...], 'send_time': '...'}
    kind='eye'  -> {'FrameNumber':..., 'PupilDiameter':..., 'Blink':..., 'send_time': '...'}

心电波形是**按心搏合成的 PQRST**（每个心搏叠 5 个高斯波：P/Q/R/S/T），并带一点
呼吸性窦性心律不齐。老写法用 0.2Hz/0.1Hz 正弦当 P/T 波、每秒来一段 6Hz 正弦当 QRS，
结果 89% 的采样点几乎不动，界面上看着就是「一条直线 + 每秒一根刺」，不像心电；
而且分析层算 HRV 时会因为心搏间期全是离群值而跳过窗口。
"""
from __future__ import annotations

import logging
import math
import random
from bisect import bisect_left
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)


class SimCollector(BaseCollector):
    """按固定频率产生模拟数据。"""

    name = 'sim'
    auto_reconnect = False

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None):
        super().__init__(config, sink)
        self.kind: str = self.config.get('kind', 'ecg')
        self.rate_hz: float = float(self.config.get('rate_hz', 3.0))          # 每秒包数
        self.samples_per_packet: int = int(self.config.get('samples_per_packet', 80))
        self.sample_rate: int = int(self.config.get('sample_rate', 240))      # 心电采样率
        self.heart_rate: float = float(self.config.get('heart_rate', 60.0))   # 合成心电的心率
        self.hrv: float = float(self.config.get('hrv', 0.04))                 # 心律不齐幅度
        self.name = self.config.get('name', f'sim_{self.kind}')
        self.device = self.config.get('device', self.kind)
        self._packet_index = 0
        self._beats: List[float] = [0.4]                # 合成心电的心搏时刻（第一个心搏在 0.4s）

    def setup(self) -> None:
        log.info('[%s] 模拟数据源启动：kind=%s，%s 包/秒', self.name, self.kind, self.rate_hz)

    def run_loop(self) -> None:
        interval = 1.0 / self.rate_hz if self.rate_hz > 0 else 0.3
        while not self.should_stop():
            if self.kind == 'eye':
                self.emit(self._make_eye(), t_device=self._packet_index / self.rate_hz)
            elif self.kind == 'grip':
                self.emit(self._make_grip(), t_device=self._packet_index / self.rate_hz)
            else:
                self.emit(self._make_ecg(), t_device=self._packet_index / self.rate_hz)
            self._packet_index += 1
            if not self.sleep(interval):
                break

    # ------------------------------------------------------------------ 造数
    def _make_ecg(self) -> Dict[str, Any]:
        """合成一段真正的 PQRST 心电（每个心搏 = P/Q/R/S/T 五个高斯波）。"""
        n = self.samples_per_packet
        start = self._packet_index * n / self.sample_rate
        end = start + n / self.sample_rate
        self._ensure_beats(end)
        values = [round(self._ecg_value(start + i / self.sample_rate) + random.gauss(0, 0.01), 4)
                  for i in range(n)]
        return {'ECG': values, 'send_time': now_str()}

    def _ensure_beats(self, until: float) -> None:
        """把心搏时刻排到 until 之后；间期按心率走，叠一点窦性心律不齐。"""
        while not self._beats or self._beats[-1] < until + 1.0:
            previous = self._beats[-1]
            rr = 60.0 / self.heart_rate * (1.0 + self.hrv * math.sin(2 * math.pi * 0.25 * previous)
                                           + random.gauss(0, 0.01))
            self._beats.append(previous + max(rr, 0.4))

    def _ecg_value(self, t: float) -> float:
        """P/Q/R/S/T 高斯叠加（幅度单位 mV，R 峰 1.0mV、T 波 0.3mV）。"""
        value = 0.0
        index = max(0, bisect_left(self._beats, t - 0.4))
        for beat in self._beats[index:index + 3]:
            if beat - t > 0.5:
                break
            value += 0.12 * self._gauss(t, beat - 0.20, 0.020)      # P
            value += -0.15 * self._gauss(t, beat - 0.025, 0.008)    # Q
            value += 1.00 * self._gauss(t, beat, 0.010)             # R
            value += -0.25 * self._gauss(t, beat + 0.030, 0.010)    # S
            value += 0.30 * self._gauss(t, beat + 0.220, 0.050)     # T
        return value

    @staticmethod
    def _gauss(t: float, mu: float, sigma: float) -> float:
        return math.exp(-((t - mu) ** 2) / (2 * sigma ** 2))

    def _make_eye(self) -> Dict[str, Any]:
        frame = self._packet_index
        return {
            'FrameNumber': frame,
            'TimeStamp': int(1e6 * frame / self.rate_hz),
            'FrameRate': float(self.rate_hz),
            'PupilDiameter': round(3.2 + 0.4 * math.sin(frame / 25.0) + random.gauss(0, 0.02), 4),
            'Blink': 1 if frame % 120 == 0 else 0,
            'Saccade': 1 if frame % 47 == 0 else 0,
            'Fixation': 1 if frame % 47 != 0 else 0,
            'send_time': now_str(),
        }

    def _make_grip(self) -> Dict[str, Any]:
        """合成一帧手套原始数据：前 3 个角度通道 + 后 15 个压力通道，左右手轮流。"""
        frame = self._packet_index
        side = 'L' if (frame // 10) % 2 == 0 else 'R'
        angles = [int(1800 * math.sin(frame / 40.0 + i)) for i in range(3)]
        base = 1500 + 900 * math.sin(frame / 25.0)
        pressures = [int(base + 200 * math.sin(frame / 7.0 + i)) for i in range(15)]
        return {
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'raw': angles + pressures,
            'battery': '87',
            'side': side,
            'send_time': now_str(),
        }
