# -*- coding: utf-8 -*-
"""实时数据缓冲 + 存储钩子。

数据流（线程边界要清楚）：
    BLE 通知回调（asyncio 线程）-> 只入队 -> SensorWriter 线程解析/入库
        -> MonitorRecorder 钩子（同一写线程）-> LiveBuffers（加锁的环形缓冲）
        -> GUI 定时器（主线程）取快照画图

界面线程不直接碰 BLE / 写库，写线程也不碰任何 Qt 对象——两边只通过这把锁交接数据，
避免跨线程操作 Qt 控件导致的访问违例（在另一个项目里踩过：0xC0000005，地址 0x8）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import numpy as np

from core.db_storage import DataRecorder

# 采样率默认值：由实测数据推出来（心电 80 点/包 × 3.125 包/秒 = 250Hz；
# 呼吸 8 点/包 × 3.125 = 25Hz；心电贴三轴 16 点/包 × 3.125 = 50Hz）
ECG_RATE = 250.0
RESP_RATE = 25.0
ECG_ACCEL_RATE = 50.0


class LiveBuffers:
    """线程安全的实时缓冲。"""

    def __init__(self, ecg_seconds: float = 12.0, ppg_seconds: float = 12.0,
                 gsr_seconds: float = 60.0):
        self.lock = threading.Lock()
        self.ecg: Deque[float] = deque(maxlen=int(ECG_RATE * ecg_seconds))
        self.resp: Deque[float] = deque(maxlen=int(RESP_RATE * ecg_seconds))
        self.ecg_accel: Dict[str, Deque[float]] = {
            axis: deque(maxlen=int(ECG_ACCEL_RATE * ecg_seconds)) for axis in 'xyz'}
        self.ppg: Dict[str, Deque[float]] = {
            f'PPG{i}': deque(maxlen=int(140 * ppg_seconds)) for i in range(1, 5)}
        self.bracelet_accel: Dict[str, Deque[float]] = {
            axis: deque(maxlen=int(20 * ppg_seconds)) for axis in 'xyz'}
        self.gsr: Deque[float] = deque(maxlen=int(10 * gsr_seconds))
        self.temp: Deque[float] = deque(maxlen=600)

        self.skin_temp: Optional[float] = None
        self.battery: Optional[Dict[str, Any]] = None
        self.last_temp: Optional[float] = None
        self.camera_frame: Optional[tuple] = None      # (jpeg, frame_index, time)

        # 计数（GUI 每 tick 读一次算速率，不入库）
        self.ecg_packets = 0
        self.ecg_samples = 0
        self.ppg_packets = 0
        self.ppg_samples = 0
        self.gsr_packets = 0
        self.gsr_samples = 0
        self.bracelet_packets = 0
        self.packet_loss = 0
        self.bad_packets = 0
        self.last_packet_no: Optional[int] = None
        self.last_ecg_at: float = 0.0
        self.last_bracelet_at: float = 0.0
        self.first_ecg_at: float = 0.0
        self.started_at: float = time.time()

    # ------------------------------------------------------------- 写入
    def publish_ecg(self, decoded: Dict[str, Any], ts: Optional[str] = None) -> None:
        """心电贴一包：ECG 80 点 + 呼吸 8 点 + 三轴 16 点 + 体温。"""
        now = time.monotonic()
        with self.lock:
            self.ecg_packets += 1
            self.last_ecg_at = now
            if not self.first_ecg_at:
                self.first_ecg_at = now

            packet_no = decoded.get('_包号')
            if isinstance(packet_no, int):
                if self.last_packet_no is not None and packet_no > self.last_packet_no + 1:
                    self.packet_loss += packet_no - self.last_packet_no - 1
                self.last_packet_no = packet_no

            ecg = decoded.get('ECG') or {}
            samples = ecg.get('ECG') or []
            if samples:
                self.ecg.extend(float(v) for v in samples)
                self.ecg_samples += len(samples)
            if '体温' in ecg:
                self.last_temp = float(ecg['体温'])
                self.temp.append(self.last_temp)

            self.resp.extend(float(v) for v in (decoded.get('呼吸') or []))
            for point in (decoded.get('三轴') or []):
                for axis in 'xyz':
                    if axis in point:
                        self.ecg_accel[axis].append(float(point[axis]))

    def publish_bracelet(self, decoded: Dict[str, Any], ts: Optional[str] = None) -> None:
        """手环一包：PPG(4 通道) / 三轴 / GSR / 肤温 / 电量。"""
        now = time.monotonic()
        with self.lock:
            self.last_bracelet_at = now
            self.bracelet_packets += 1

            samples = decoded.get('PPG') or []
            if samples:
                self.ppg_packets += 1
                for point in samples:
                    for index in range(1, 5):
                        key = f'PPG{index}'
                        if key in point:
                            self.ppg[key].append(float(point[key]))
                    self.ppg_samples += 1

            accel = decoded.get('腕部三轴') or {}
            for axis in 'xyz':
                if axis in accel:
                    self.bracelet_accel[axis].append(float(accel[axis]))

            gsr = decoded.get('GSR') or []
            if gsr:
                self.gsr_packets += 1
                self.gsr_samples += len(gsr)
                self.gsr.extend(float(v) for v in gsr)

            if '肤温' in decoded:
                self.skin_temp = float(decoded['肤温'])
            if decoded.get('电量'):
                self.battery = dict(decoded['电量'])

    def publish_camera_frame(self, jpeg: bytes, frame_index: int, stamp: str = '') -> None:
        """摄像头帧（回放时从同一个会话库里读出来的）。"""
        with self.lock:
            self.camera_frame = (jpeg, int(frame_index), stamp)

    # ------------------------------------------------------------- 读取
    @staticmethod
    def _arr(values: Deque[float]) -> List[float]:
        return list(values)

    def snapshot(self) -> Dict[str, Any]:
        """GUI 每 30~50ms 取一次；只做浅拷贝，很快。"""
        with self.lock:
            now = time.monotonic()
            return {
                'ecg': self._arr(self.ecg),
                'resp': self._arr(self.resp),
                'ecg_accel': {axis: self._arr(values) for axis, values in self.ecg_accel.items()},
                'ppg': {key: self._arr(values) for key, values in self.ppg.items()},
                'bracelet_accel': {axis: self._arr(values)
                                   for axis, values in self.bracelet_accel.items()},
                'gsr': self._arr(self.gsr),
                'temp': self._arr(self.temp),
                'skin_temp': self.skin_temp,
                'battery': dict(self.battery) if self.battery else None,
                'temp_last': self.last_temp,
                'camera_frame': self.camera_frame,
                'counters': {
                    'ecg_packets': self.ecg_packets,
                    'ecg_samples': self.ecg_samples,
                    'ppg_packets': self.ppg_packets,
                    'ppg_samples': self.ppg_samples,
                    'gsr_packets': self.gsr_packets,
                    'gsr_samples': self.gsr_samples,
                    'bracelet_packets': self.bracelet_packets,
                    'packet_loss': self.packet_loss,
                },
                'ages': {
                    'ecg': (now - self.last_ecg_at) if self.last_ecg_at else None,
                    'bracelet': (now - self.last_bracelet_at) if self.last_bracelet_at else None,
                },
                'elapsed': now - self.started_at,
            }

    def reset(self) -> None:
        with self.lock:
            for values in (self.ecg, self.resp, self.gsr, self.temp):
                values.clear()
            for values in self.ecg_accel.values():
                values.clear()
            for values in self.ppg.values():
                values.clear()
            for values in self.bracelet_accel.values():
                values.clear()
            self.packet_loss = 0
            self.last_packet_no = None


class MonitorRecorder(DataRecorder):
    """在原有「入库」之外，顺手把解析结果推给界面缓冲。

    只覆盖两个分流方法（心电贴 / 手环），接收模块完全不用改。
    """

    def __init__(self, storage, buffers: LiveBuffers):
        super().__init__(storage)
        self.buffers = buffers

    def record(self, decoded, ts=None):               # noqa: D102 - 与父类一致
        try:
            super().record(decoded, ts)
        finally:
            self.buffers.publish_ecg(decoded, ts)

    def record_bracelet(self, decoded, ts=None):      # noqa: D102
        try:
            super().record_bracelet(decoded, ts)
        finally:
            self.buffers.publish_bracelet(decoded, ts)


def ecg_signal_quality(ecg: np.ndarray, sample_rate: float = ECG_RATE):
    """判心电信号质量，返回 (状态, 说明)。

    实测踩到的坑：电极没贴好时，设备照样按 250Hz 推数据，但值在 ±1 上下抖
    （开头还可能有一段 ±32000 的脱落瞬态）。这种"有数据但没信号"的情况下
    如果硬算心率，就会把噪声当成 QRS，报出 190+ 的假心率。
    """
    x = np.asarray(ecg, dtype=float) if ecg is not None else np.array([])
    if x.size < sample_rate * 2:
        return 'wait', '等待数据…'
    if np.mean(np.abs(x) > 32000) > 0.02:
        return 'bad', '信号饱和（电极可能脱落）'
    p_high, p_low = np.percentile(x, 99.5), np.percentile(x, 0.5)
    if p_high - p_low < 50:
        return 'weak', '信号过弱（检查电极接触）'
    return 'ok', '信号正常'


def estimate_heart_rate(ecg: np.ndarray, sample_rate: float = ECG_RATE) -> Optional[float]:
    """从最近一段心电估心率；信号不可用时返回 None（界面显示 --）。"""
    quality, _text = ecg_signal_quality(ecg, sample_rate)
    if quality != 'ok':
        return None
    if ecg is None or len(ecg) < sample_rate * 3:
        return None
    x = np.asarray(ecg, dtype=float)

    # 差分移动平均 ≈ 3~15Hz 带通：压掉基线漂移和高频噪声，突出 QRS
    fast = max(2, int(0.012 * sample_rate))
    slow = max(fast + 2, int(0.08 * sample_rate))
    kernel_fast = np.ones(fast) / fast
    kernel_slow = np.ones(slow) / slow
    y = np.convolve(x, kernel_fast, mode='same') - np.convolve(x, kernel_slow, mode='same')

    amplitude = np.percentile(np.abs(y), 99)
    if amplitude < 5:
        return None
    threshold = 0.45 * amplitude
    refractory = int(0.25 * sample_rate)
    peaks: List[int] = []
    last = -refractory
    for i in range(1, len(y) - 1):
        if y[i] > threshold and y[i] >= y[i - 1] and y[i] >= y[i + 1] and i - last > refractory:
            peaks.append(i)
            last = i
    if len(peaks) < 3:
        return None
    rr = np.diff(peaks) / sample_rate
    if len(rr) >= 2 and np.std(rr) / max(np.mean(rr), 1e-6) > 0.35:
        return None                                    # 间期太乱，不像窦性心律
    bpm = float(60.0 / np.median(rr))
    return bpm if 30.0 <= bpm <= 200.0 else None


def resp_has_signal(resp: np.ndarray) -> bool:
    """呼吸通道是否真的有波形（实测有设备把这一路固定上报 0）。"""
    if resp is None or len(resp) < 20:
        return False
    x = np.asarray(resp, dtype=float)
    return bool(np.percentile(x, 99) - np.percentile(x, 1) > 0.5)


def estimate_resp_rate(resp: np.ndarray, sample_rate: float = RESP_RATE) -> Optional[float]:
    """呼吸率（次/分）：15 秒窗口内数峰值。"""
    if resp is None or len(resp) < sample_rate * 6:
        return None
    x = np.asarray(resp, dtype=float)
    x = x - np.mean(x)
    amplitude = np.percentile(np.abs(x), 99)
    if amplitude <= 0:
        return None
    threshold = 0.3 * amplitude
    refractory = int(1.2 * sample_rate)
    peaks, last = 0, -refractory
    for i in range(1, len(x) - 1):
        if x[i] > threshold and x[i] >= x[i - 1] and x[i] >= x[i + 1] and i - last > refractory:
            peaks += 1
            last = i
    duration = len(x) / sample_rate
    if duration <= 0:
        return None
    rate = peaks * 60.0 / duration
    return rate if 3.0 <= rate <= 60.0 else None
