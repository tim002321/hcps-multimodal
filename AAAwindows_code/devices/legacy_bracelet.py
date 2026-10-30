#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绿光手环独立调试脚本（**主程序不引用**，保留供现场排查用）。

历史来源：这份文件原本叫 `shouhuan.py`，是手环协议刚拿到时的验证程序，
自带扫描/连接/串口透传/文档示例解析四种玩法。主流程（`main.py` /
`run_monitor.py`）只用其中的 `ProtocolParser`，其余部分留在主文件里会
让人误以为它们在跑，故拆到此处。

用法（单独运行，不写库）：

    python devices/legacy_bracelet.py                 # 蓝牙连接模式
    python devices/legacy_bracelet.py --demo          # 用协议文档里的示例字节验证解析
    python devices/legacy_bracelet.py --serial COM3   # 串口透传模式
    python devices/legacy_bracelet.py --name MPPB20000008 --scan-timeout 20

只想看解析结果是否正确 → 直接用 `--demo`，不需要任何硬件。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time

# 允许直接 `python devices/legacy_bracelet.py` 运行：
# 直接运行脚本时 sys.path[0] 是 devices/，拿不到包名，这里补上项目根目录。
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devices.bracelet_protocol import COMMON_NOTIFY_UUIDS, ProtocolParser   # noqa: E402

# 现场手环的默认广播名
DEVICE_NAME = 'MPPB20000008'


class BluetoothReceiver:
    """扫描 → 连接 → 订阅 Notify → 把字节喂给 ProtocolParser。"""

    def __init__(self, parser: ProtocolParser, device_name: str = DEVICE_NAME,
                 notify_uuid: str = None, scan_timeout: float = 10.0):
        self.parser = parser
        self.device_name = device_name
        self.notify_uuid = notify_uuid
        self.scan_timeout = scan_timeout
        self.client = None
        self.running = False

    async def scan_device(self):
        """扫描附近 BLE 设备，返回名称匹配的那个（找不到返回 None）。"""
        from bleak import BleakScanner

        print(f'[蓝牙] 正在扫描设备 (名称: {self.device_name}) ...')
        print(f'[蓝牙] 扫描超时: {self.scan_timeout}s')

        devices = await BleakScanner.discover(timeout=self.scan_timeout)
        for device in devices:
            name = device.name or ''
            print(f'[蓝牙] 发现设备: {name} ({device.address})')
            if name.strip() == self.device_name:
                print(f'[蓝牙] ✓ 找到目标设备: {name} ({device.address})')
                return device

        print(f"[蓝牙] ✗ 未找到名称为 '{self.device_name}' 的设备")
        return None

    def _find_notify_characteristic(self):
        """定位通知特征：指定 UUID → 常见透传 UUID → 任意 notify/indicate。"""
        candidates = []

        if self.notify_uuid:
            for service in self.client.services:
                for char in service.characteristics:
                    if char.uuid.lower() == self.notify_uuid.lower():
                        return char.uuid
            print(f'[蓝牙] ⚠ 指定的特征 {self.notify_uuid} 未找到，尝试自动发现')

        for common_uuid in COMMON_NOTIFY_UUIDS:
            for service in self.client.services:
                for char in service.characteristics:
                    if char.uuid.lower() == common_uuid.lower():
                        return char.uuid

        for service in self.client.services:
            for char in service.characteristics:
                props = [prop.lower() for prop in char.properties]
                if 'notify' in props or 'indicate' in props:
                    candidates.append((char.uuid, service.uuid, char.properties))

        if not candidates:
            return None

        print(f'[蓝牙] 发现 {len(candidates)} 个可订阅特征:')
        for uuid, service_uuid, props in candidates:
            print(f'        {uuid} (service={service_uuid}, props={props})')

        for uuid, _service_uuid, props in candidates:      # 优先 Notify，其次 Indicate
            if 'notify' in [prop.lower() for prop in props]:
                return uuid
        return candidates[0][0]

    async def connect_and_receive(self):
        """完整流程：扫描 → 连接 → 发现服务 → 订阅特征 → 保持接收。"""
        from bleak import BleakClient

        device = await self.scan_device()
        if device is None:
            return False

        print(f'[蓝牙] 正在连接 {device.address} ...')
        self.client = BleakClient(device.address)
        try:
            await self.client.connect()
        except Exception as exc:
            print(f'[蓝牙] 连接失败: {exc}')
            return False

        if not self.client.is_connected:
            print('[蓝牙] 连接未建立')
            return False

        print(f'[蓝牙] ✓ 已连接 {device.name} ({device.address})')

        char_uuid = self._find_notify_characteristic()
        if not char_uuid:
            print('[蓝牙] ✗ 未找到可订阅的特征，断开连接')
            await self.client.disconnect()
            return False

        print(f'[蓝牙] 订阅特征: {char_uuid}')

        def on_data(_sender, data: bytearray):
            self.parser.feed(bytes(data))

        await self.client.start_notify(char_uuid, on_data)
        print('[蓝牙] ✓ 数据接收已启动，等待数据 ...\n')
        self.running = True

        try:
            while self.running:
                if not self.client.is_connected:
                    print('[蓝牙] 连接已断开')
                    break
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

        return True

    async def _cleanup(self):
        self.running = False
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
                print('[蓝牙] 已断开连接')
            except Exception:
                pass

    async def disconnect(self):
        """主动断开连接。"""
        self.running = False
        await self._cleanup()


class SerialReceiver:
    """串口透传接收器（适用于蓝牙模块被映射成虚拟串口的情况）。"""

    def __init__(self, parser: ProtocolParser, port: str = 'COM3', baud: int = 115200):
        self.parser = parser
        self.port = port
        self.baud = baud
        self.ser = None
        self.running = False

    def start(self):
        import serial

        self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        self.running = True
        print(f'[串口] 已打开 {self.port} @ {self.baud}')

        while self.running:
            try:
                data = self.ser.read(1024)
                if data:
                    self.parser.feed(data)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f'[串口错误] {exc}')

        self.stop()

    def stop(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
            print('[串口] 已关闭')


# ---------------------------------------------------------------- 演示
def print_callback(data):
    """通用打印回调。"""
    print(f'[{time.strftime("%H:%M:%S")}] {data}')


def demo_from_hex_file(hex_file: str):
    """从十六进制文本文件模拟数据接收（离线分析用）。"""
    parser = ProtocolParser(
        on_ppg=print_callback,
        on_gsr=print_callback,
        on_temp=print_callback,
        on_battery=print_callback,
    )

    with open(hex_file, 'r', encoding='utf-8') as handle:
        for line in handle:
            hex_bytes = re.findall(r'[0-9A-Fa-f]{2}', line.strip())
            if hex_bytes:
                parser.feed(bytes(int(item, 16) for item in hex_bytes))

    print('\n=== 统计 ===')
    print(f"接收字节: {parser.stats['recv_bytes']}")
    print(f"解析帧数: {parser.stats['parsed']}")
    print(f"错误帧数: {parser.stats['errors']}")


# 《绿光手环数据协议》文档里的示例帧
DOC_SAMPLES = {
    'ppg_accel': '00 AA 00 CC 20 00 00 01 30 00 01 00 DF DE EC 08 F8 C8 55 10 DF BD AE 18 EF 4A B1 '
                 '00 E5 6D BB 08 FA 50 22 10 E3 E7 7C 18 FB 1F 12 00 EC 97 77 08 F7 C4 A0 10 E6 14 6B '
                 '18 F1 54 63 00 F2 C3 54 08 F0 0E 20 10 EA 77 51 18 DF 69 85 00 F2 D8 E0 08 EC FC 2C '
                 '10 EC 81 08 18 DA 5E 22 00 F2 0F 26 08 ED 3F 0F 10 EB A6 05 18 DD CD 9E 00 F1 9A EF '
                 '08 EF 7B BF 10 E9 3D 22 18 E7 3F 8D 00 EF 0B 65 08 F1 F1 BB 10 E7 4F E5 18 F1 19 0C '
                 '00 EE 41 56 08 F2 F7 59 10 E6 B7 AB 18 F5 40 A2 00 EE FC 03 08 F2 27 37 10 E7 2C 04 '
                 '18 F3 7B A6 FD B4 03 6A F8 FF',
    # 文档 GSR 示例：帧头4 + 设备ID4 + 类型1 + 20 字节 GSR = 29 字节
    'gsr': '00 AA 00 CC 20 00 00 00 21 03 08 04 00 04 03 04 05 04 07 05 00 05 05 04 08 04 09 05 00',
    # 肤温示例：00 AA 00 CC 00 00 00 01 33 09 D4 00
    'temp': '00 AA 00 CC 00 00 00 01 33 09 D4 00',
    # 电量示例：00 AA 00 CC 00 00 00 01 34 09 D4
    'battery': '00 AA 00 CC 00 00 00 01 34 09 D4',
}


def demo_with_doc_samples():
    """用协议文档里的示例帧验证解析结果。"""
    parser = ProtocolParser(
        on_ppg=print_callback,
        on_gsr=print_callback,
        on_temp=print_callback,
        on_battery=print_callback,
    )

    for hex_string in DOC_SAMPLES.values():
        hex_bytes = re.findall(r'[0-9A-Fa-f]{2}', hex_string)
        parser.feed(bytes(int(item, 16) for item in hex_bytes))

    print('\n=== 统计 ===')
    print(f"接收字节: {parser.stats['recv_bytes']}")
    print(f"解析帧数: {parser.stats['parsed']}")
    print(f"错误帧数: {parser.stats['errors']}")


# ---------------------------------------------------------------- 入口
def _parse_cli(argv):
    """极简参数解析：保持与原脚本完全一致的选项。"""
    options = {'mode': 'ble', 'device_name': DEVICE_NAME, 'notify_uuid': None,
               'scan_timeout': 30.0, 'serial_port': 'COM3'}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--demo':
            options['mode'] = 'demo'
        elif arg == '--serial':
            options['mode'] = 'serial'
            if index + 1 < len(argv) and not argv[index + 1].startswith('--'):
                options['serial_port'] = argv[index + 1]
                index += 1
        elif arg == '--ble':
            options['mode'] = 'ble'
        elif arg in ('--name', '--uuid', '--scan-timeout') and index + 1 < len(argv):
            value = argv[index + 1]
            index += 1
            if arg == '--name':
                options['device_name'] = value
            elif arg == '--uuid':
                options['notify_uuid'] = value
            else:
                options['scan_timeout'] = float(value)
        index += 1
    return options


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print('=' * 60)
    print('  绿光手环数据接收与解析（独立调试脚本）')
    print('=' * 60)
    print('  1) 蓝牙连接: python devices/legacy_bracelet.py [--name X] [--uuid X] [--scan-timeout N]')
    print('  2) 文档示例: python devices/legacy_bracelet.py --demo')
    print('  3) 串口透传: python devices/legacy_bracelet.py --serial COM3')
    print()

    options = _parse_cli(argv)

    if options['mode'] == 'demo':
        demo_with_doc_samples()
        return 0

    parser = ProtocolParser(
        on_ppg=print_callback,
        on_gsr=print_callback,
        on_temp=print_callback,
        on_battery=print_callback,
    )

    if options['mode'] == 'serial':
        try:
            SerialReceiver(parser, port=options['serial_port']).start()
        except KeyboardInterrupt:
            print('\n[程序] 用户中断')
        return 0

    receiver = BluetoothReceiver(
        parser,
        device_name=options['device_name'],
        notify_uuid=options['notify_uuid'],
        scan_timeout=options['scan_timeout'],
    )
    try:
        asyncio.run(receiver.connect_and_receive())
    except KeyboardInterrupt:
        print('\n[程序] 用户中断，正在断开连接 ...')
        try:
            asyncio.run(receiver.disconnect())
        except Exception:
            pass
        print('[程序] 已退出')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
