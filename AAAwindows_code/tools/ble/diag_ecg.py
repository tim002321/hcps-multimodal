#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""心电贴连接分级诊断：把「一直显示正在连接」拆成可定位的阶段。

用法（在主程序所在机器上运行，**先关掉主程序**，否则连接会被占用）：

    python tools/ble/diag_ecg.py
    python tools/ble/diag_ecg.py --name E1220511000413 --listen 10
    python tools/ble/diag_ecg.py --all          # 扫描时列出全部广播

它做的是主程序 run_device() 同一套动作，但每一步都带独立超时和计时，
卡在哪一步就停在哪一步并报出来：

  ① 扫描 → ② 建链(connect) → ③ 服务发现(client.services) →
  ④ 找通知/命令特征 → ⑤ 下发开始命令 → ⑥ 订阅并统计实际收到的包

主程序里对应位置：
  devices/ecg_receiver.py:242  await client.connect()
  devices/ecg_receiver.py:247  find_notify_char()  ← 返回 None 时静默 continue
  devices/ecg_receiver.py:264  find_command_char() ← 返回 None 时静默 continue

只连接、只读、不写库，退出时主动断开。
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import os
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SERVICE_UUID = '554b0001-15e1-4af1-a6e3-588089f20b68'
HANDLE_ENABLE_1 = 20
HANDLE_ENABLE_2 = 25
HANDLE_DATA = 22
ENABLE_NOTIFY = b'\x01\x00'
CMD_END_COLLECT = bytearray([0x5B, 0xC1, 0x00, 0x00, 0x23, 0xFE, 0x5D])
CMD_START_COLLECT = bytearray([0x5B, 0xC0, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x41, 0x5C, 0x5D])
PACKET_SIZE = 244


def load_ecg_names() -> list:
    parser = configparser.ConfigParser()
    parser.read(os.path.join(ROOT, 'config.ini'), encoding='utf-8')
    raw = parser.get('bluetooth', 'device_names', fallback='')
    return [n.strip() for n in raw.split(',') if n.strip()]


def address_kind(address: str) -> str:
    """按首字节低 2 位判断 BLE 地址类型（公共 / 静态随机 / 可解析私有 / 不可解析私有）。"""
    try:
        first = int(address.replace(':', '')[:2], 16)
    except ValueError:
        return '无法解析'
    return {
        0b00: '公共地址（厂商 OUI，全球唯一）',
        0b01: '静态随机地址（设备自生成，上电后固定）',
        0b10: '可解析私有地址（约 15 分钟轮换一次）',
        0b11: '不可解析私有地址（定期轮换）',
    }[first & 0b11]


def stage(no: int, text: str) -> float:
    print('\n[{}] {}'.format(no, text), flush=True)
    return time.perf_counter()


def elapsed(t0: float) -> str:
    return '{:.2f}s'.format(time.perf_counter() - t0)


async def diagnose(args) -> int:
    names = [n for n in ([args.name] if args.name else load_ecg_names()) if n]
    if not names:
        print('[错误] config.ini 里 bluetooth.device_names 为空，且未传 --name')
        return 2
    print('目标设备名：{}'.format(', '.join(names)))

    # ---------------------------------------------------------------- ① 扫描
    t0 = stage(1, '扫描广播（{:.0f} 秒）'.format(args.scan))
    found = await BleakScanner.discover(timeout=args.scan, return_adv=True)
    target = None
    if args.all:
        print('  {:<24} {:<20} {:>6}'.format('广播名', '地址', 'RSSI'))
        for dev, adv in found.values():
            print('  {:<24} {:<20} {:>6}'.format((dev.name or '')[:24], dev.address, adv.rssi))
    for dev, adv in found.values():
        name = dev.name or getattr(adv, 'local_name', '') or ''
        if name in names:
            target = (dev, adv, name)
            break
    print('  共 {} 个广播，用时 {}'.format(len(found), elapsed(t0)))
    if target is None:
        print('\n[结论] 没扫到目标设备 → 心电贴没开机、走远了，或蓝牙适配器异常。')
        print('       这一步失败与「连接」无关，请先确认设备在广播。')
        return 1

    device, adv, name = target
    print('  命中：{}  {}  {} dBm'.format(name, device.address, adv.rssi))
    print('  地址类型：{}'.format(address_kind(device.address)))

    client = BleakClient(device, timeout=args.connect_timeout)

    # ---------------------------------------------------------------- ② 建链
    t0 = stage(2, '建立连接（超时 {:.0f}s）'.format(args.connect_timeout))
    try:
        await client.connect()
        print('  connect() 返回，用时 {}，MTU={}'.format(elapsed(t0), client.mtu_size))
        print('  服务数量：{}（此值非 0 说明服务发现已完成）'.format(len(list(client.services))))
    except asyncio.TimeoutError:
        print('  [超时] {} 内没有连上 —— 典型原因是链路被其它主机占用，'.format(elapsed(t0)))
        print('         或设备侧尚未释放上一条连接（supervision timeout 4~6 秒）。')
        return 1
    except Exception as exc:                      # noqa: BLE001
        print('  [异常] {}: {}'.format(type(exc).__name__, exc))
        print('  用时 {}'.format(elapsed(t0)))
        return 1

    try:
        # ------------------------------------------------------ ③ 服务发现
        t0 = stage(3, '服务发现（超时 {:.0f}s，主程序此处没有超时）'.format(args.services_timeout))
        try:
            services = await asyncio.wait_for(
                asyncio.to_thread(lambda: list(client.services)), timeout=args.services_timeout)
            print('  完成，{} 个服务，用时 {}'.format(len(services), elapsed(t0)))
        except asyncio.TimeoutError:
            print('  [挂起] {} 内服务表没返回。'.format(elapsed(t0)))
            print('         这会让主程序永远停在「正在连接」——它在这步没有超时保护。')
            return 1

        for svc in services:
            mark = '  <<< 目标服务' if svc.uuid.lower() == SERVICE_UUID else ''
            print('  服务 {} [{}]{}'.format(svc.description, svc.uuid, mark))
            for ch in svc.characteristics:
                print('    0x{:04X}  {}  {}'.format(ch.handle, ch.uuid, ','.join(ch.properties)))

        # ------------------------------------------------------ ④ 找特征
        stage(4, '定位通知 / 命令特征')
        notify_char = None
        for svc in services:
            if svc.uuid.lower() == SERVICE_UUID and svc.characteristics:
                notify_char = svc.characteristics[0]
                break
        if notify_char is None:
            for svc in services:
                for ch in svc.characteristics:
                    if 'notify' in ch.properties:
                        notify_char = ch
                        break
                if notify_char:
                    break
        if notify_char is None:
            print('  [缺失] 没有任何可通知的特征。')
            print('         主程序此时会静默 continue，界面就一直是黄色「连接中」。')
            return 1
        print('  通知特征 0x{:04X} {} {}'.format(
            notify_char.handle, notify_char.uuid, ','.join(notify_char.properties)))

        cmd_char = None
        try:
            cmd_char = client.services.get_characteristic(HANDLE_DATA)
        except Exception:                         # noqa: BLE001
            cmd_char = None
        if cmd_char is not None:
            print('  命令特征（按句柄 22）0x{:04X} {}'.format(cmd_char.handle, cmd_char.uuid))
        else:
            for svc in services:
                if svc.uuid.lower() != SERVICE_UUID:
                    continue
                for ch in svc.characteristics:
                    if 'write' in ch.properties or 'write-without-response' in ch.properties:
                        cmd_char = ch
                        break
            if cmd_char is None:
                print('  [缺失] 目标服务内没有可写特征。')
                print('         主程序同样会静默 continue（界面保持黄色）。')
            else:
                print('  命令特征（回退匹配）0x{:04X} {}'.format(cmd_char.handle, cmd_char.uuid))

        # ------------------------------------------------------ ⑤ 握手
        t0 = stage(5, '下发开始采集命令')
        for handle in (HANDLE_ENABLE_1, HANDLE_ENABLE_2):
            try:
                await client.write_gatt_char(handle, ENABLE_NOTIFY, response=False)
                print('  写句柄 {} 成功'.format(handle))
            except Exception as exc:              # noqa: BLE001
                print('  写句柄 {} 失败（主程序里这步被 pass 吞掉）：{}'.format(handle, exc))
        if cmd_char is not None:
            for label, cmd in (('结束采集', CMD_END_COLLECT), ('开始采集', CMD_START_COLLECT)):
                try:
                    await asyncio.wait_for(
                        client.write_gatt_char(cmd_char, cmd, response=True),
                        timeout=args.att_timeout)
                    print('  {} 命令已下发'.format(label))
                    await asyncio.sleep(0.1)
                except asyncio.TimeoutError:
                    print('  [挂起] {} 命令 {} 秒内没有 ATT 响应。'.format(label, args.att_timeout))
                    print('         主程序此处无超时，会永远停在「正在连接」。')
                    return 1
                except Exception as exc:          # noqa: BLE001
                    print('  {} 命令下发失败（主程序同样被 pass 吞掉）：{}'.format(label, exc))
        print('  用时 {}'.format(elapsed(t0)))

        # ------------------------------------------------------ ⑥ 收包
        t0 = stage(6, '订阅通知并统计 {} 秒内实收数据'.format(args.listen))
        frames = []
        chunks = []

        def on_notify(_sender, data):
            chunks.append(len(data))
            frames.append(bytes(data))

        try:
            await asyncio.wait_for(client.start_notify(notify_char, on_notify),
                                   timeout=args.att_timeout)
        except asyncio.TimeoutError:
            print('  [挂起] start_notify {} 秒内没有收到 CCCD 写响应。'.format(args.att_timeout))
            print('         写 CCCD 也是 ATT 请求，设备不回应就会一直等。')
            return 1
        await asyncio.sleep(args.listen)
        try:
            await asyncio.wait_for(client.stop_notify(notify_char), timeout=args.att_timeout)
        except Exception:                         # noqa: BLE001
            pass

        print('  通知条数 {}，累计字节 {}，用时 {}'.format(
            len(frames), sum(chunks), elapsed(t0)))
        if not frames:
            print('\n[结论] 链路和订阅都成功，但设备一个包都没发。')
            print('       重点查：开始命令是否被设备接受、电极是否贴在身上。')
            return 1

        print('  单条通知长度分布：{}'.format(sorted({len(f) for f in frames})[:8]))
        joined = b''.join(frames)
        pos = joined.find(b'\x5B')
        print('  前 16 字节：{}'.format(joined[:16].hex(' ').upper()))
        if len(joined) >= PACKET_SIZE and joined[0] == 0x5B and joined[PACKET_SIZE - 1] == 0x5D:
            pkt = joined[:PACKET_SIZE]
            print('  首包：包头 0x5B / 包尾 0x5D 正确')
            print('    指令 0x{:02X}  类型字1 0x{:02X}  类型字2 0x{:02X}  类型字3 0x{:02X}'.format(
                pkt[1], pkt[8], pkt[173], pkt[191]))
            print('    loff(电极脱落标志, offset 10) = 0x{:02X}'.format(pkt[10]))
            print('    体温 = {:.2f} °C'.format(struct.unpack_from('<h', pkt, 11)[0] / 100.0))
            ecg = struct.unpack_from('<80h', pkt, 13)
            print('    前 8 个采样：{}'.format(list(ecg[:8])))
            print('    采样峰峰值 = {}'.format(max(ecg) - min(ecg)))
            print('    CRC16(原始) = {}'.format(pkt[241:243].hex().upper()))
        else:
            print('  [异常] 收到的字节流不是 244 字节、包头包尾对齐的数据包。')
            print('         起始字节偏移 {}，请把上面这行十六进制发回来。'.format(pos))
            return 1

        print('\n[结论] 从扫描到收包全链路正常。若主程序仍显示正在连接，')
        print('       问题在主程序的状态机（静默 continue 分支）而不是设备。')
        return 0
    finally:
        try:
            if client.is_connected:
                await client.disconnect()
                print('\n已断开连接')
        except Exception:                         # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description='心电贴连接分级诊断')
    parser.add_argument('--name', default='', help='指定一个广播名，默认取 config.ini 的全部')
    parser.add_argument('--scan', type=float, default=12.0, help='扫描秒数')
    parser.add_argument('--connect-timeout', type=float, default=10.0)
    parser.add_argument('--services-timeout', type=float, default=20.0)
    parser.add_argument('--att-timeout', type=float, default=8.0,
                        help='单个 ATT 请求（写命令 / 写 CCCD）的等待上限')
    parser.add_argument('--listen', type=float, default=8.0, help='订阅后统计数据的秒数')
    parser.add_argument('--all', action='store_true', help='扫描时列出全部广播')
    args = parser.parse_args()
    try:
        return asyncio.run(diagnose(args))
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
