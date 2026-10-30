#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绿光手环接收模块（Windows / bleak），对接主程序统一存储。

对外接口：
    async def start_bracelet(recorder, device_names, scan_timeout, debug=False,
                             notify_uuid=None, start_delay=3.0,
                             reconnect_interval=5.0, rescan_interval=10.0)

数据流向：
    扫描 → 连接 → 订阅 notify → 组包解析 → recorder.record_bracelet(...) → SQLite（同一数据库）

设计说明：
  - 协议解析复用 `shouhuan.py`（ProtocolParser / GSRData / SkinTempData / BatteryData），
    仅把 PPG(0x30) 包扩展为「整包 4 通道 × 10 个采样点」解析，见 BraceletParser。
  - 本模块“只写数据库”：控制台仅打印「已连接」与「设备掉线」两类日志，
    不逐包打印、不写 JSONL、不打印扫描过程与统计。
  - 通知回调只做字节拷贝并 `writer.submit(...)` 入队，解析与入库都在写库线程执行，
    避免阻塞事件循环导致 BLE 通知被整条丢弃（详见 `writer_thread.py` 说明）。
  - 写入的表由 `db_storage.DataRecorder.record_bracelet` 分流：
        bracelet_ppg_data / bracelet_accel_data / bracelet_gsr_data /
        bracelet_skin_temp / bracelet_battery
  - 手环连接后无需下发采集命令，直接订阅通知特征即可（协议文档仅定义上行数据）。
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from bleak import BleakClient

from core.db_storage import DataRecorder
from devices.ble_utils import dump_services, iter_characteristics, scan_for_names
from devices.bracelet_protocol import COMMON_NOTIFY_UUIDS, ProtocolParser

# ---------------------------------------------------------------------------
# 手环 PPG(0x30) 载荷结构（见《绿光手环数据协议》）
#   载荷 166 字节 = 160 字节 PPG + 6 字节腕部三轴
#   PPG: 每 4 字节为一个通道采样(1 字节状态 + 3 字节/24bit 数据)，
#        每 16 字节组成 1 个采样点(4 通道)，共 10 个采样点 → 250sps = 25 包/秒 × 10
# ---------------------------------------------------------------------------
PPG_CHANNELS = 4
PPG_BYTES_PER_CHANNEL_SAMPLE = 4
PPG_PAYLOAD_BYTES = 160
PPG_SAMPLES_PER_PACKET = PPG_PAYLOAD_BYTES // (PPG_CHANNELS * PPG_BYTES_PER_CHANNEL_SAMPLE)  # 10
ACCEL_PAYLOAD_BYTES = 6
ACCEL_SCALE = 32 / 65535.0  # 文档公式: x = data * 32 / 65535.0

# 重连/重扫默认参数
DEFAULT_START_DELAY = 3.0        # 启动延迟，避免与心电模块同时发起 BLE 扫描
DEFAULT_RECONNECT_INTERVAL = 5.0  # 断开后重连间隔（秒）
DEFAULT_RESCAN_INTERVAL = 10.0    # 未扫描到设备时的重扫间隔（秒）


@dataclass
class PPGPacketData:
    """整包 PPG + 腕部三轴数据：4 通道 × 10 采样点 + 1 组三轴。"""

    device_id: int
    count: int
    samples: List[dict]          # [{"PPG1":.., "PPG2":.., "PPG3":.., "PPG4":..}, ...] 共 10 个
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0

    def __repr__(self):
        return (
            "PPG[dev={:#010x}] cnt={} 采样点={} 首点={} ACC=({:.3f}, {:.3f}, {:.3f})".format(
                self.device_id, self.count, len(self.samples),
                self.samples[0] if self.samples else None,
                self.accel_x, self.accel_y, self.accel_z,
            )
        )


class BraceletParser(ProtocolParser):
    """在 shouhuan.ProtocolParser 基础上，把 PPG 包解析为整包 10 个采样点 × 4 通道。

    GSR / 肤温 / 电量 三类仍沿用父类解析逻辑（回调参数为对应的数据类实例）。
    """

    def _parse_ppg_accel(self, frame: bytes, dev_id: int, count: int, payload_offset: int):
        payload = frame[payload_offset:]
        ppg_raw = payload[:PPG_PAYLOAD_BYTES]
        accel_raw = payload[PPG_PAYLOAD_BYTES:PPG_PAYLOAD_BYTES + ACCEL_PAYLOAD_BYTES]

        # ---- PPG: 10 个采样点 × 4 通道，24bit 有符号 ----
        samples = []
        for s in range(PPG_SAMPLES_PER_PACKET):
            base = s * PPG_CHANNELS * PPG_BYTES_PER_CHANNEL_SAMPLE
            point = {}
            for ch in range(PPG_CHANNELS):
                off = base + ch * PPG_BYTES_PER_CHANNEL_SAMPLE
                # off 为状态字节(忽略)，后 3 字节为数据（文档公式 /256 等价于 24bit 有符号）
                raw24 = (ppg_raw[off + 1] << 16) | (ppg_raw[off + 2] << 8) | ppg_raw[off + 3]
                if raw24 & 0x800000:
                    raw24 -= 0x1000000
                point["PPG{}".format(ch + 1)] = raw24
            samples.append(point)

        # ---- 腕部三轴: 每轴 2 字节，int16 大端 ----
        accels = []
        for i in range(3):
            val = (accel_raw[i * 2] << 8) | accel_raw[i * 2 + 1]
            if val & 0x8000:
                val -= 0x10000
            accels.append(val * ACCEL_SCALE)

        result = PPGPacketData(
            device_id=dev_id, count=count, samples=samples,
            accel_x=accels[0], accel_y=accels[1], accel_z=accels[2],
        )
        if self.on_ppg:
            self.on_ppg(result)


class BraceletReceiver:
    """手环接收器：扫描 → 连接 → 订阅 → 解析 → 入库（断线自动重连）。"""

    def __init__(self, recorder: DataRecorder, device_names, scan_timeout: float = 10.0,
                 debug: bool = False, notify_uuid: Optional[str] = None,
                 reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL,
                 writer=None, status=None):
        self.recorder = recorder
        self.device_names = [n for n in (device_names or []) if n]
        self.scan_timeout = scan_timeout
        self.debug = debug
        self.notify_uuid = notify_uuid or None
        self.reconnect_interval = reconnect_interval
        # 写库线程（SensorWriter）：非空时通知回调只入队，解析+入库都在该线程执行
        self.writer = writer
        self.status = status
        self._received_at = None                      # 当前包的通知到达时刻

    # ---------------------------------------------------------------
    # 扫描
    # ---------------------------------------------------------------
    async def scan(self, attempt: int = 1, attempts: int = 10, misses: int = 0):
        """扫描附近的 BLE 设备，返回名称匹配的手环设备列表（不打印）。"""
        if self.status is not None:
            if misses < attempts:
                detail = '第 {}/{} 次扫描（每次 {:g} 秒）'.format(
                    attempt, attempts, self.scan_timeout)
                state = 'scanning'
            else:
                detail = '第 {} 次扫描（已连续 {} 次未找到，继续重试…）'.format(attempt, misses)
                state = 'not_found'
            self.status.set('bracelet', state, '', detail)
        # 公共扫描逻辑（bleak 3.x 的 return_adv=True 在 scan_for_names 内部处理）
        return [device for device, _rssi in await scan_for_names(self.device_names, self.scan_timeout)]

    # ---------------------------------------------------------------
    # 通知特征定位
    # ---------------------------------------------------------------
    def find_notify_char(self, client: BleakClient):
        """定位可订阅的通知特征：指定 UUID → 常见透传 UUID → 任意 notify/indicate 特征。"""
        chars = [char for _service, char in iter_characteristics(client)]

        if self.notify_uuid:
            target = self.notify_uuid.lower()
            for char in chars:
                if char.uuid.lower() == target:
                    return char

        for common in COMMON_NOTIFY_UUIDS:
            for char in chars:
                if char.uuid.lower() == common.lower():
                    return char

        candidates = [
            char for char in chars
            if "notify" in [p.lower() for p in char.properties]
            or "indicate" in [p.lower() for p in char.properties]
        ]
        if not candidates:
            return None

        if self.debug:
            for char in candidates:
                print(
                    "[手环] 候选特征 0x{:04X} {} {}".format(
                        char.handle, char.uuid, ",".join(char.properties)
                    ),
                    flush=True,
                )

        # 优先 Notify，其次 Indicate
        for char in candidates:
            if "notify" in [p.lower() for p in char.properties]:
                return char
        return candidates[0]

    # ---------------------------------------------------------------
    # 数据回调：统一交给 recorder 分流入库（每个数据包共享一个时间戳）
    # ---------------------------------------------------------------
    def _on_ppg(self, pkt: PPGPacketData):
        self.recorder.record_bracelet({
            "PPG": pkt.samples,
            "腕部三轴": {
                "x": round(pkt.accel_x, 6),
                "y": round(pkt.accel_y, 6),
                "z": round(pkt.accel_z, 6),
            },
        }, ts=self._received_at)

    def _on_gsr(self, gsr):
        self.recorder.record_bracelet({"GSR": gsr.samples}, ts=self._received_at)

    def _on_temp(self, temp):
        self.recorder.record_bracelet({"肤温": round(temp.temperature, 3)}, ts=self._received_at)

    def _on_battery(self, battery):
        self.recorder.record_bracelet({
            "电量": {"adc": battery.raw_adc, "percent": round(battery.percentage, 1)},
        }, ts=self._received_at)

    # ---------------------------------------------------------------
    # 单设备连接循环（断开后自动重连）
    # ---------------------------------------------------------------
    async def run_device(self, device):
        address = device.address
        name = device.name or ""
        tag = "[手环:{}]".format(name or address)

        # 解析器在整个生命周期内复用，统计与缓冲区状态得以保留
        parser = BraceletParser(
            on_ppg=self._on_ppg,
            on_gsr=self._on_gsr,
            on_temp=self._on_temp,
            on_battery=self._on_battery,
        )

        while True:
            client = BleakClient(device)
            if self.status is not None:
                self.status.set('bracelet', 'connecting', name or address,
                                '正在连接 {}'.format(address))
            try:
                await client.connect()

                if self.debug:
                    dump_services(client, tag='手环')

                notify_char = self.find_notify_char(client)
                if notify_char is None:
                    if self.debug:
                        dump_services(client, tag='手环')
                    await client.disconnect()
                    await asyncio.sleep(self.reconnect_interval)
                    continue

                # 重连后清空残留的半包数据，避免跨连接拼接出错
                parser.buffer.clear()

                def process(payload, received_at):
                    """解析 + 入库：在写库线程里执行（无 writer 时退回事件循环内执行）。"""
                    # 时间戳用通知到达时刻；写库线程是串行的，这里存一下给解析回调用
                    self._received_at = received_at
                    parser.feed(payload)

                if self.writer is not None:
                    def on_notify(_sender, data):
                        # 回调内只做字节拷贝和入队，不做任何耗时操作
                        payload = bytes(data)
                        received_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                        self.writer.submit(lambda: process(payload, received_at))
                else:
                    def on_notify(_sender, data):
                        process(bytes(data), datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'))

                await client.start_notify(notify_char, on_notify)

                # 唯一保留的两类日志之一：连接成功
                print(
                    "{} 已连接并开始接收 ({}, MTU={}, 通知特征 0x{:04X})".format(
                        tag, address, client.mtu_size, notify_char.handle
                    ),
                    flush=True,
                )
                if self.status is not None:
                    self.status.set('bracelet', 'connected', name or address,
                                    '{}  MTU={}'.format(address, client.mtu_size))

                # 保持连接，直到断开或被取消
                while client.is_connected:
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("{} 连接异常: {}".format(tag, e), flush=True)
                if self.status is not None:
                    self.status.set('bracelet', 'error', name or address, str(e))
            finally:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass

            print("{} 设备已掉线，{} 秒后重连...".format(tag, self.reconnect_interval), flush=True)
            if self.status is not None:
                self.status.set('bracelet', 'lost', name or address,
                                '已掉线，{} 秒后重连'.format(self.reconnect_interval))
            await asyncio.sleep(self.reconnect_interval)

    # ---------------------------------------------------------------
    # 资源清理
    # ---------------------------------------------------------------
    async def disconnect(self):
        """占位：连接对象由 run_device 内部管理并自动清理。"""
        return


async def start_bracelet(recorder, device_names, scan_timeout: float = 10.0, debug: bool = False,
                         notify_uuid: Optional[str] = None,
                         start_delay: float = DEFAULT_START_DELAY,
                         reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL,
                         rescan_interval: float = DEFAULT_RESCAN_INTERVAL,
                         writer=None, status=None, scan_attempts: int = 10):
    """扫描手环设备并启动接收任务（由主程序作为 asyncio 任务启动）。

    未配置设备名称时直接返回；未扫描到设备时按 `rescan_interval` 周期重扫（不打印）。
    `writer` 为写库线程（SensorWriter），传入后通知回调只入队、解析与入库在写线程执行。
    """
    names = [n for n in (device_names or []) if n]
    if not names:
        return

    # 延迟启动，避免与心电模块同时发起 BLE 扫描（Windows 蓝牙栈并发扫描可能相互影响）
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    receiver = BraceletReceiver(
        recorder, names, scan_timeout, debug, notify_uuid, reconnect_interval,
        writer=writer, status=status
    )

    attempts = max(1, int(scan_attempts))
    misses = 0
    while True:
        attempt = misses + 1
        try:
            matched = await receiver.scan(attempt, attempts, misses)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[手环] 扫描失败: {}".format(e), flush=True)
            matched = []

        if not matched:
            misses += 1
            if misses >= attempts and status is not None:
                # 连续 attempts 次都没扫到才算"无法连接"；之后仍然继续重试
                status.set('bracelet', 'not_found', '',
                           '已连续 {} 次扫描未找到手环，仍在重试…'.format(misses))
            await asyncio.sleep(rescan_interval)
            continue

        misses = 0
        # 已找到的设备由 run_device 内部负责断线重连（同一地址长期有效）
        tasks = [asyncio.create_task(receiver.run_device(dev)) for dev in matched]
        await asyncio.gather(*tasks)
