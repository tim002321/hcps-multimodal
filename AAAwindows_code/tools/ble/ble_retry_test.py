#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证"连续扫描 N 次才报无法连接"的逻辑（不需要设备）。

用一个不存在的设备名 + 很短的扫描超时，跑几秒看状态怎么变：
应当在第 1~N 次时显示"第 k/N 次扫描"，到第 N 次之后才变成"未找到"。

用法：
    python tools/ble_retry_test.py
    python tools/ble_retry_test.py --attempts 10 --timeout 0.5 --seconds 6
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from devices.ecg_receiver import PacketOutput, start_receiver   # noqa: E402
from core.device_status import DeviceStatus                      # noqa: E402


async def run(args) -> int:
    status = DeviceStatus()
    task = asyncio.create_task(start_receiver(
        None, ['__NO_SUCH_DEVICE__'], args.timeout, PacketOutput(None), False, None,
        status=status, scan_attempts=args.attempts, scan_interval=args.interval))
    seen = []
    deadline = asyncio.get_event_loop().time() + args.seconds
    try:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)
            item = status.get('ecg')
            line = '{} | {}'.format(item.get('state'), item.get('detail'))
            if not seen or seen[-1] != line:
                seen.append(line)
                print('  {}'.format(line))
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    not_found_index = next((i for i, line in enumerate(seen) if line.startswith('not_found')), None)
    ok = not_found_index is not None and any(
        '第 1/{} 次'.format(args.attempts) in line for line in seen[:max(1, not_found_index)])
    print('\n[{}] 第 {} 次扫描后才报未找到'.format('通过' if ok else '失败',
                                               (not_found_index or 0) and not_found_index + 1))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='BLE 重试逻辑自检')
    parser.add_argument('--attempts', type=int, default=10)
    parser.add_argument('--timeout', type=float, default=0.5)
    parser.add_argument('--interval', type=float, default=0.1)
    parser.add_argument('--seconds', type=float, default=7.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
