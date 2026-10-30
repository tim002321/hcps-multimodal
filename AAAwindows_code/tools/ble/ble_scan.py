#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描附近 BLE 广播，标出 config.ini 里配置的心电贴/手环是否在位。

用法：
    python tools/ble_scan.py --seconds 8

只扫描，不连接、不写库，用来确认设备有没有开机、名字对不对、信号强度如何。
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import os

from bleak import BleakScanner

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_names():
    parser = configparser.ConfigParser()
    parser.read(os.path.join(ROOT, 'config.ini'), encoding='utf-8')
    ecg = [n.strip() for n in parser.get('bluetooth', 'device_names', fallback='').split(',')
           if n.strip()]
    bracelet = [n.strip() for n in parser.get('bracelet', 'bracelet_names', fallback='').split(',')
                if n.strip()]
    return ecg, bracelet


async def main() -> int:
    parser = argparse.ArgumentParser(description='BLE 广播扫描')
    parser.add_argument('--seconds', type=float, default=8.0)
    parser.add_argument('--all', action='store_true', help='列出所有设备（默认只列配置里匹配的）')
    args = parser.parse_args()

    ecg_names, bracelet_names = load_names()
    print('心电贴配置：{}'.format(', '.join(ecg_names) or '（空）'))
    print('手环配置：{}'.format(', '.join(bracelet_names) or '（空）'))
    print('扫描 {:.0f} 秒…'.format(args.seconds))

    found = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
    ecg_hit, bracelet_hit = [], []
    print('\n{:<28} {:<20} {:>6}'.format('广播名', '地址', 'RSSI'))
    print('-' * 60)
    for device, adv in found.values():
        name = device.name or adv.local_name or ''
        if not name and not args.all:
            continue
        tag = ''
        if name in ecg_names:
            tag = '  <- 心电贴'
            ecg_hit.append(name)
        elif name in bracelet_names:
            tag = '  <- 手环'
            bracelet_hit.append(name)
        elif not args.all:
            continue
        print('{:<28} {:<20} {:>6}{}'.format(name or '(无名)', device.address, adv.rssi, tag))

    print('\n心电贴：{}'.format('已找到 ' + ', '.join(ecg_hit) if ecg_hit else '未找到'))
    print('手环：{}'.format('已找到 ' + ', '.join(bracelet_hit) if bracelet_hit else '未找到'))
    return 0 if (ecg_hit or bracelet_hit) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
