#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝牙心电数据接收模块（供 main.py / run_monitor.py 调用，不单独运行）。

功能：
  1. 扫描并匹配心电蓝牙设备（按广播名称）
  2. 连接设备，下发开始采集命令
  3. 订阅通知，接收 244 字节心电数据包
  4. 按原始协议解码 ECG（心电）/ 呼吸 / 三轴加速度 / 体温
  5. 可选写入 JSONL 文件，并通过 DataRecorder 写入数据库
     （控制台只打印「连接成功」与「设备掉线」两类日志，不打印采集数据）

对外接口：start_receiver(recorder, device_names, scan_timeout, output, debug, writer=None)
底层 BLE 库：bleak（Windows 使用 WinRT 后端，替代原代码中仅支持 Linux 的 bluepy）

运行方式：python main.py（纯采集） 或 python run_monitor.py（带界面）
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
from datetime import datetime

from bleak import BleakClient

from core.db_storage import DataRecorder
from devices.ble_utils import dump_services, scan_for_names

# ---------------------------------------------------------------------------
# 协议常量（与原代码保持一致）
# ---------------------------------------------------------------------------
# 通知数据所在的服务 UUID
SERVICE_UUID = "554b0001-15e1-4af1-a6e3-588089f20b68"

# 完整数据包大小（原始字节）。原代码中 hexlify 后为 488 字节，
# 对应原始数据 244 字节，此处直接按原始字节处理。
PACKET_SIZE = 244

# 包头 / 包尾
HEADER = 0x5B
TAIL = 0x5D

# 握手命令（与原代码一致）
ENABLE_NOTIFY = b"\x01\x00"
CMD_END_COLLECT = bytearray([0x5B, 0xC1, 0x00, 0x00, 0x23, 0xFE, 0x5D])     # 结束采集
CMD_START_COLLECT = bytearray([0x5B, 0xC0, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x41, 0x5C, 0x5D])  # 开始采集

# 原代码中用于下发命令 / 使能通知的 GATT 属性句柄
HANDLE_ENABLE_1 = 20
HANDLE_ENABLE_2 = 25
HANDLE_DATA = 22


# ---------------------------------------------------------------------------
# 数据包解码
# ---------------------------------------------------------------------------
def decode_ecg_packet(data: bytes):
    """
    解码 244 字节心电数据包。

    包格式（偏移 : 字段）：
      0      包头 0x5B
      1      指令类型
      2      包长度
      3      未知
      4:8    包号 (uint32 big-endian)
      8      类型字1 (0x01 = ECG 心电)
      9      类型1长度
      10     loff
      11:13  体温 (int16 little-endian) / 100.0
      13:173 80 个 ECG 采样点 (int16 little-endian, 有符号)
      173    类型字2 (0x03 = 呼吸)
      174    类型2长度
      175:191 8 个呼吸采样点 (int16 little-endian, 有符号)
      191    类型字3 (0x02 = 三轴加速度)
      192    类型3长度 (字节数，除以 3 为采样点数)
      193:209 x 轴 (int8) / 64.0
      209:225 y 轴 (int8) / 64.0
      225:241 z 轴 (int8) / 64.0
      241:243 CRC16
      243    包尾 0x5D

    返回 dict，解码失败返回 None。
    """
    if len(data) != PACKET_SIZE:
        return None
    if data[0] != HEADER or data[-1] != TAIL:
        return None

    result = {}

    # 包号
    packet_no = struct.unpack_from(">I", data, 4)[0]

    # 类型字1：ECG
    if data[8] == 0x01:
        temperature = struct.unpack_from("<h", data, 11)[0] / 100.0
        ecg = list(struct.unpack_from("<80h", data, 13))
        result["ECG"] = {"体温": temperature, "ECG": ecg}

    # 类型字2：呼吸
    if data[173] == 0x03:
        breathe = list(struct.unpack_from("<8h", data, 175))
        result["呼吸"] = breathe

    # 类型字3：三轴加速度
    if data[191] == 0x02:
        count = data[192] // 3
        x_axis = struct.unpack_from("<{}b".format(count), data, 193)
        y_axis = struct.unpack_from("<{}b".format(count), data, 209)
        z_axis = struct.unpack_from("<{}b".format(count), data, 225)
        triaxial = [
            {"x": x_axis[i] / 64.0, "y": y_axis[i] / 64.0, "z": z_axis[i] / 64.0}
            for i in range(count)
        ]
        result["三轴"] = triaxial

    if not result:
        return None

    # 附上元信息，便于排查丢包 / 顺序问题
    result["_包号"] = packet_no
    result["_CRC16"] = data[241:243].hex().upper()
    return result


class PacketAssembler:
    """
    将分片 / 粘包的通知字节流重组为完整的 244 字节数据包。
    Windows 蓝牙栈若未协商到足够大的 MTU，设备会把一个包拆成多条通知，
    因此需要缓冲并按包头/包尾对齐重组。
    """

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk: bytes):
        """喂入一段字节，返回其中解析出的完整数据包列表。"""
        self.buffer.extend(chunk)
        packets = []
        while len(self.buffer) >= PACKET_SIZE:
            if self.buffer[0] == HEADER:
                if self.buffer[PACKET_SIZE - 1] == TAIL:
                    packets.append(bytes(self.buffer[:PACKET_SIZE]))
                    del self.buffer[:PACKET_SIZE]
                else:
                    # 包头正确但包尾不对齐，向后找下一个包头
                    idx = self.buffer.find(bytes([HEADER]), 1)
                    if idx == -1:
                        del self.buffer[:PACKET_SIZE]
                    else:
                        del self.buffer[:idx]
            else:
                idx = self.buffer.find(bytes([HEADER]))
                if idx == -1:
                    self.buffer.clear()
                else:
                    del self.buffer[:idx]
        return packets


# ---------------------------------------------------------------------------
# 输出处理
# ---------------------------------------------------------------------------
class PacketOutput:
    """数据输出：可选写入 JSONL 文件（不再向控制台打印数据）。

    控制台只保留连接成功 / 设备掉线两类日志（见 run_device、start_receiver）。
    """

    def __init__(self, out_file=None):
        self.out_file = out_file

    def emit(self, address, decoded):
        if not self.out_file:
            return
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "device": address,
            "data": decoded,
        }
        try:
            with open(self.out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            print("[错误] 写文件失败: {}".format(e), file=sys.stderr)


# ---------------------------------------------------------------------------
# BLE 连接与监听
# ---------------------------------------------------------------------------
async def find_notify_char(client: BleakClient):
    """找到用于接收通知的特征（优先原代码指定的服务第一个特征）。"""
    for service in client.services:
        if service.uuid.lower() == SERVICE_UUID:
            chars = service.characteristics
            if chars:
                return chars[0]
    # 兜底：任意一个具有 notify 属性的特征
    for service in client.services:
        for char in service.characteristics:
            if "notify" in char.properties:
                return char
    return None


def find_command_char(client: BleakClient):
    """找到用于下发采集命令（开始/结束采集）的特征。

    优先使用原代码中的固定句柄 HANDLE_DATA(22)；若在 Windows 上句柄不一致，
    则在目标服务内查找可写特征作为命令特征。
    """
    ch = client.services.get_characteristic(HANDLE_DATA)
    if ch is not None:
        return ch

    for service in client.services:
        if service.uuid.lower() == SERVICE_UUID:
            for char in service.characteristics:
                if "write" in char.properties or "write-without-response" in char.properties:
                    return char
    return None


async def run_device(device, output: PacketOutput, debug: bool, recorder: DataRecorder,
                     writer=None, rssi=None, status=None):
    """连接单个设备并持续接收通知，断开后自动重连。

    `writer` 为写库线程（SensorWriter）：传入后通知回调只入队，
    组包/解码/入库/打印/写 JSONL 都在写线程执行，避免阻塞事件循环。
    `rssi` 为扫描阶段的信号强度（dBm），仅用于连接日志展示。
    """
    address = device.address
    while True:
        client = BleakClient(device)
        if status is not None:
            status.set('ecg', 'connecting', device.name or address, '正在连接 {}'.format(address))
        try:
            await client.connect()

            if debug:
                dump_services(client, tag='ecg')

            notify_char = await find_notify_char(client)
            if notify_char is None:
                await client.disconnect()
                await asyncio.sleep(3)
                continue

            # 握手流程：
            #   - handle 20 / 25 写 0x0100 是 bluepy 时代手动使能 CCCD 的做法，
            #     bleak 的 start_notify 会自动写 CCCD，故此步在 Windows 上可省略，失败无碍。
            #   - handle 22（命令特征）下发“结束采集”+“开始采集”，必不可少。
            for h in (HANDLE_ENABLE_1, HANDLE_ENABLE_2):
                try:
                    await client.write_gatt_char(h, ENABLE_NOTIFY, response=False)
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

            cmd_char = find_command_char(client)
            if cmd_char is None:
                if debug:
                    dump_services(client, tag='ecg')
                await client.disconnect()
                await asyncio.sleep(3)
                continue

            try:
                await client.write_gatt_char(cmd_char, CMD_END_COLLECT, response=True)
                await asyncio.sleep(0.1)
                await client.write_gatt_char(cmd_char, CMD_START_COLLECT, response=True)
            except Exception:
                pass

            # 订阅通知
            assembler = PacketAssembler()

            def process(payload, received_at):
                """组包 + 解码 + 输出 + 入库：在写库线程里执行。"""
                for pkt in assembler.feed(payload):
                    decoded = decode_ecg_packet(pkt)
                    if decoded is not None:
                        output.emit(address, decoded)
                        # 用"通知到达时刻"，而不是写库线程处理到它的时刻，
                        # 否则队列有积压时心电时间轴会相对摄像头整体后移
                        recorder.record(decoded, ts=received_at)

            if writer is not None:
                def on_notify(sender, data):
                    # 回调内只做字节拷贝和入队，不做任何耗时操作
                    payload = bytes(data)
                    received_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                    writer.submit(lambda: process(payload, received_at))
            else:
                def on_notify(sender, data):
                    process(bytes(data), datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'))

            await client.start_notify(notify_char, on_notify)

            # 唯一保留的两类日志之一：连接成功
            rssi_text = "{} dBm".format(rssi) if rssi is not None else "未知"
            print(
                "[{}] 已连接并开始接收 (MTU={}, 通知特征 0x{:04X}, 信号强度={})".format(
                    address, client.mtu_size, notify_char.handle, rssi_text
                ),
                flush=True,
            )
            if status is not None:
                status.set('ecg', 'connected', device.name or address,
                           '{}  MTU={}  信号 {}'.format(address, client.mtu_size, rssi_text))

            # 保持连接
            while client.is_connected:
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[{}] 连接异常: {}".format(address, e), flush=True)
            if status is not None:
                status.set('ecg', 'error', device.name or address, str(e))
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

        # 唯一保留的两类日志之二：设备掉线
        print("[{}] 设备已掉线，3 秒后重连...".format(address), flush=True)
        if status is not None:
            status.set('ecg', 'lost', device.name or address, '已掉线，3 秒后重连')
        await asyncio.sleep(3)


async def start_receiver(recorder, device_names, scan_timeout, output, debug, writer=None,
                         status=None, scan_attempts=10, scan_interval=2.0):
    """扫描设备并启动匹配设备的监听任务（由主程序作为任务启动）。

    `writer` 为写库线程（SensorWriter），传入后通知处理全部在写线程执行。
    扫描过程不打印；仅匹配成功后由 run_device 打印「已连接」/「掉线」。

    扫描策略：单次扫描失败或没扫到都不算失败，连续 `scan_attempts` 次
    没扫到才把状态标成「未找到」，之后仍按 `scan_interval` 继续重试
    （设备开机后会自动接上，不需要重启程序）。
    """
    attempts = max(1, int(scan_attempts))
    misses = 0

    while True:
        attempt = misses + 1

        if status is not None:
            if misses < attempts:
                detail = '第 {}/{} 次扫描（每次 {:g} 秒）'.format(attempt, attempts, scan_timeout)
                state = 'scanning'
            else:
                detail = '第 {} 次扫描（已连续 {} 次未找到，继续重试…）'.format(attempt, misses)
                state = 'not_found'
            status.set('ecg', state, '', detail)

        try:
            matched = await scan_for_names(device_names, scan_timeout)
        except Exception as exc:                  # noqa: BLE001 - 单次扫描失败继续下一次
            misses += 1
            print('[ecg] 扫描失败（第 {} 次）：{}'.format(attempt, exc), flush=True)
            if status is not None and misses >= attempts:
                status.set('ecg', 'error', '', '连续 {} 次扫描失败：{}'.format(misses, exc))
            await asyncio.sleep(scan_interval)
            continue

        if matched:
            misses = 0
            tasks = [
                asyncio.create_task(run_device(dev, output, debug, recorder, writer, rssi, status))
                for dev, rssi in matched
            ]
            await asyncio.gather(*tasks)
            continue

        misses += 1
        if misses >= attempts and status is not None:
            # 连续 attempts 次都没扫到才算"无法连接"；之后仍然继续重试
            status.set('ecg', 'not_found', '',
                       '已连续 {} 次扫描未找到心电贴，仍在重试…'.format(misses))
        await asyncio.sleep(scan_interval)
