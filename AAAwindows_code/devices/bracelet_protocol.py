# -*- coding: utf-8 -*-
"""绿光手环协议解析（纯字节 → 数据类，不涉及蓝牙连接，可离线单测）。

支持的数据类型（帧头统一为 00 AA 00 CC）：

======  ======  ========  ==============================================
类型字  长度    周期      含义
======  ======  ========  ==============================================
0x30    177B    40ms      4 通道绿光 PPG（每通道 24bit）+ 腕部三轴
0x21    29B     1s        皮电 GSR（10 个 16bit 样本）
0x33    12B     10s       肤温（CT1711 数字量）
0x34    11B     —         电量（ADC → 百分比）
======  ======  ========  ==============================================

帧结构：
    0..3    帧头 00 AA 00 CC
    4..7    设备 ID（uint32 小端）
    8       数据类型
    9..10   计数戳（仅 PPG 有，uint16 小端）
    11..    载荷

主程序只用这里的 `ProtocolParser`；早期那份独立调试脚本在
`legacy_bracelet.py`，不走主流程。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional

__all__ = [
    'FRAME_HEADER', 'COMMON_NOTIFY_UUIDS',
    'TYPE_PPG_ACCEL', 'TYPE_GSR', 'TYPE_SKIN_TEMP', 'TYPE_BATTERY',
    'LEN_PPG_ACCEL', 'LEN_GSR', 'LEN_SKIN_TEMP', 'LEN_BATTERY',
    'PPGAccelData', 'GSRData', 'SkinTempData', 'BatteryData',
    'ProtocolParser',
]

# ============================================================
# 1. 协议常量
# ============================================================

# 公共帧头
FRAME_HEADER = bytes([0x00, 0xAA, 0x00, 0xCC])

# 数据类型
TYPE_PPG_ACCEL = 0x30   # 4 通道 PPG + 腕部三轴加速度
TYPE_GSR = 0x21         # 皮电数据
TYPE_SKIN_TEMP = 0x33   # 肤温数据
TYPE_BATTERY = 0x34     # 手环电量

# 各类型数据包总长度（含帧头）
LEN_PPG_ACCEL = 177     # 帧头4 + 设备ID4 + 类型1 + 计数2 + PPG160 + 加速度6
LEN_GSR = 29            # 帧头4 + 设备ID4 + 类型1 + GSR20
LEN_SKIN_TEMP = 12      # 帧头4 + 设备ID4 + 类型1 + 温度2 + 空1
LEN_BATTERY = 11        # 帧头4 + 设备ID4 + 类型1 + ADC2

# 帧结构偏移
OFF_HEADER = 0          # 帧头 4 字节
OFF_DEV_ID = 4          # 设备 ID 4 字节
OFF_DATA_TYPE = 8       # 数据类型 1 字节
OFF_COUNT = 9           # 计数戳 2 字节
OFF_PAYLOAD = 11        # 载荷起始

# 手环常用的透传 Notify 特征（找不到时由接收端退回「任意 notify/indicate 特征」）
COMMON_NOTIFY_UUIDS = [
    '0000ffe1-0000-1000-8000-00805f9b34fb',   # 常见透传特征
    '6e400003-b5a3-f393-e0a9-e50e24dcca9e',   # Nordic UART RX
    '0000ffb2-0000-1000-8000-00805f9b34fb',
]


# ============================================================
# 2. 解析结果（数据类）
# ============================================================

@dataclass
class PPGAccelData:
    """4 通道 PPG + 腕部三轴加速度（单包）。"""

    device_id: int
    count: int
    ppg1: int
    ppg2: int
    ppg3: int
    ppg4: int
    accel_x: float
    accel_y: float
    accel_z: float

    def __repr__(self):
        return (f"PPG[dev={self.device_id:#010x}] cnt={self.count} | "
                f"PPG1={self.ppg1} PPG2={self.ppg2} PPG3={self.ppg3} PPG4={self.ppg4} | "
                f"ACC=({self.accel_x:.3f}, {self.accel_y:.3f}, {self.accel_z:.3f})")


@dataclass
class GSRData:
    """皮电（GSR）：一包 10 个电导值。"""

    device_id: int
    samples: List[float]

    def __repr__(self):
        return f"GSR[dev={self.device_id:#010x}] values={self.samples}"


@dataclass
class SkinTempData:
    """肤温：原始数字量 + 摄氏度。"""

    device_id: int
    raw: int
    temperature: float

    def __repr__(self):
        return (f"TEMP[dev={self.device_id:#010x}] raw={self.raw:#06x} "
                f"temp={self.temperature:.2f}°C")


@dataclass
class BatteryData:
    """电量：ADC 原始值 + 换算后的百分比。"""

    device_id: int
    raw_adc: int
    percentage: float

    def __repr__(self):
        return (f"BAT[dev={self.device_id:#010x}] adc={self.raw_adc} "
                f"percent={self.percentage:.1f}%")


# ============================================================
# 3. 协议解析核心
# ============================================================

class ProtocolParser:
    """按帧头分包并解析各类数据。

    用法：反复调用 `feed(bytes)`，每解析出一帧就回调对应的 on_xxx；
    分包/粘包由内部缓冲区 `buffer` 处理，重连时调用方负责 `buffer.clear()`。
    """

    def __init__(self, on_ppg=None, on_gsr=None, on_temp=None, on_battery=None):
        self.buffer = bytearray()
        self.on_ppg = on_ppg
        self.on_gsr = on_gsr
        self.on_temp = on_temp
        self.on_battery = on_battery
        self.stats = {'recv_bytes': 0, 'parsed': 0, 'errors': 0}

    # ---------------------------------------------------------- 分包
    def feed(self, data: bytes) -> None:
        """喂入原始蓝牙数据，自动分包解析。"""
        self.buffer.extend(data)
        self.stats['recv_bytes'] += len(data)

        while True:
            idx = self.buffer.find(FRAME_HEADER)           # 找帧头
            if idx < 0:
                # 没有完整帧头，保留最后 3 字节防止帧头被截断
                if len(self.buffer) > 3:
                    self.buffer = self.buffer[-3:]
                break

            if idx > 0:                                    # 丢掉帧头之前的无效数据
                self.buffer = self.buffer[idx:]

            # 至少要有 帧头4 + 设备ID4 + 类型1 = 9 字节才能判断类型
            if len(self.buffer) < 9:
                break

            data_type = self.buffer[OFF_DATA_TYPE]
            layout = self._layout(data_type)
            if layout is None:                             # 未知类型：跳过 1 字节继续找
                self.buffer = self.buffer[1:]
                self.stats['errors'] += 1
                continue

            payload_offset, pkt_len = layout
            if len(self.buffer) < pkt_len:                 # 数据不够一帧，等更多数据
                break

            frame = bytes(self.buffer[:pkt_len])
            self.buffer = self.buffer[pkt_len:]

            try:
                self._parse_frame(frame, data_type, payload_offset)
                self.stats['parsed'] += 1
            except Exception as exc:                       # 单帧失败不影响后续帧
                self.stats['errors'] += 1
                print(f'[解析错误] {exc}, frame={frame.hex(" ")}')

    @staticmethod
    def _layout(data_type: int):
        """返回 (载荷偏移, 整帧长度)；未知类型返回 None。

        PPG 帧比其他三类多 2 字节计数戳，所以载荷从 11 开始、其余从 9 开始。
        """
        if data_type == TYPE_PPG_ACCEL:
            return 11, LEN_PPG_ACCEL
        if data_type == TYPE_GSR:
            return 9, LEN_GSR
        if data_type == TYPE_SKIN_TEMP:
            return 9, LEN_SKIN_TEMP
        if data_type == TYPE_BATTERY:
            return 9, LEN_BATTERY
        return None

    def _get_packet_length(self, data_type: int) -> Optional[int]:
        """按类型取整帧长度（保留给外部排查用）。"""
        layout = self._layout(data_type)
        return layout[1] if layout else None

    # ---------------------------------------------------------- 分发
    def _parse_frame(self, frame: bytes, data_type: int, payload_offset: int) -> None:
        device_id = struct.unpack_from('<I', frame, OFF_DEV_ID)[0]
        count = 0
        if data_type == TYPE_PPG_ACCEL:
            count = struct.unpack_from('<H', frame, OFF_COUNT)[0]

        if data_type == TYPE_PPG_ACCEL:
            self._parse_ppg_accel(frame, device_id, count, payload_offset)
        elif data_type == TYPE_GSR:
            self._parse_gsr(frame, device_id, payload_offset)
        elif data_type == TYPE_SKIN_TEMP:
            self._parse_temp(frame, device_id, payload_offset)
        elif data_type == TYPE_BATTERY:
            self._parse_battery(frame, device_id, payload_offset)

    # ---------------------------------------------------------- 各类解析
    def _parse_ppg_accel(self, frame: bytes, device_id: int, count: int,
                         payload_offset: int) -> None:
        """载荷 = 160 字节 PPG（4 通道 × 10 点）+ 6 字节加速度。

        每通道 4 字节：1 字节状态 + 3 字节 24bit 数据；
        文档公式 `(b0<<24 | b1<<16 | b2<<8) / 256` 等价于 24 位有符号整数。
        """
        payload = frame[payload_offset:]
        ppg_raw = payload[:160]
        accel_raw = payload[160:166]

        ppg_values = []
        for index in range(4):
            base = index * 4                              # 1 字节状态 + 3 字节数据
            b0, b1, b2 = ppg_raw[base + 1], ppg_raw[base + 2], ppg_raw[base + 3]
            raw24 = (b0 << 16) | (b1 << 8) | b2
            if raw24 & 0x800000:
                raw24 -= 0x1000000
            ppg_values.append(raw24)

        accels = []
        for index in range(3):                            # 每轴 2 字节，int16 大端
            value = (accel_raw[index * 2] << 8) | accel_raw[index * 2 + 1]
            if value & 0x8000:
                value -= 0x10000
            accels.append(value * 32 / 65535.0)            # 文档公式 → g 值

        if self.on_ppg:
            self.on_ppg(PPGAccelData(
                device_id=device_id, count=count,
                ppg1=ppg_values[0], ppg2=ppg_values[1],
                ppg3=ppg_values[2], ppg4=ppg_values[3],
                accel_x=accels[0], accel_y=accels[1], accel_z=accels[2],
            ))

    def _parse_gsr(self, frame: bytes, device_id: int, payload_offset: int) -> None:
        """皮电：载荷每 2 字节一个 16bit 样本，按实际长度动态解析。

        文档公式：fSkinElec = (2048 - n*2048/32768) / (n*2048/32768*499) * 1000
        """
        payload = frame[payload_offset:]
        samples = []
        for index in range(len(payload) // 2):
            raw = (payload[index * 2] << 8) | payload[index * 2 + 1]
            if raw == 0:
                samples.append(0.0)
                continue
            scaled = raw * 2048.0 / 32768
            numerator = 2048 - scaled
            denominator = scaled * 499
            samples.append(round(numerator / denominator * 1000 if denominator else 0.0, 3))

        if self.on_gsr:
            self.on_gsr(GSRData(device_id=device_id, samples=samples))

    def _parse_temp(self, frame: bytes, device_id: int, payload_offset: int) -> None:
        """肤温：2 字节数字量（CT1711），temp = 3.90625 * raw / 1000。"""
        payload = frame[payload_offset:]
        if len(payload) < 2:
            return
        raw = (payload[0] << 8) | payload[1]
        if raw & 0x8000:
            raw -= 0x10000
        if self.on_temp:
            self.on_temp(SkinTempData(device_id=device_id, raw=raw,
                                      temperature=3.90625 * raw / 1000.0))

    def _parse_battery(self, frame: bytes, device_id: int, payload_offset: int) -> None:
        """电量：rate = (adc*6.6/4095 - 3.3) * 100 / 0.8，钳制到 [0, 100]。"""
        payload = frame[payload_offset:]
        if len(payload) < 2:
            return
        raw_adc = (payload[0] << 8) | payload[1]
        rate = (raw_adc * 6.6 / (2 ** 12 - 1) - 3.3) * 100 / 0.8
        if self.on_battery:
            self.on_battery(BatteryData(device_id=device_id, raw_adc=raw_adc,
                                        percentage=max(0, min(100, rate))))
