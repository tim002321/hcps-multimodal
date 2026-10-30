#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据采集入口（无界面）：只连设备、只写库 / 可选写 JSONL。

运行：
    python main.py                                  # 读 config.ini
    python main.py --device E1220511000413          # 临时覆盖心电设备名（逗号分隔）
    python main.py --bracelet ""                    # 只要心电，不连手环
    python main.py --out none                       # 只入库，不写 JSONL

结构（改目录后）：
    core/config.py        读 config.ini → Config
    core/db_storage.py    SQLite 存储
    core/writer_thread.py 写库线程
    devices/*.py          各设备接收模块（统一 start_xxx 协程）

要带界面看波形，请用 `python run_monitor.py`，它复用同一套接收与入库代码。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from typing import Optional

from core.preflight import check_dependencies

check_dependencies(need_ui=False)         # 缺 bleak/numpy 时给出指引而不是 traceback

from core.config import Config
from core.db_storage import DataRecorder, SensorDB
from core.writer_thread import SensorWriter
from devices.bracelet_receiver import start_bracelet
from devices.ecg_receiver import PacketOutput, start_receiver


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='蓝牙心电 / 手环采集与存储')
    parser.add_argument('--device', '-d',
                        help='心电设备广播名称，多个用英文逗号分隔（覆盖 config.ini）')
    parser.add_argument('--bracelet', '-b',
                        help='手环设备广播名称，多个用英文逗号分隔（传 "" 表示不连手环）')
    parser.add_argument('--scan', '-s', type=float, help='扫描时长（秒）')
    parser.add_argument('--out', '-o',
                        help='输出 JSONL 路径；传 none 表示不写文件')
    parser.add_argument('--debug', action='store_true', help='连接后打印服务/特征表')
    return parser.parse_args(argv)


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    """把命令行参数覆盖到配置上；没传的项保持 config.ini 的值。"""
    changes = {}

    if args.device is not None:
        changes['ecg_names'] = [n.strip() for n in args.device.split(',') if n.strip()]
    if args.bracelet is not None:
        changes['bracelet_names'] = [n.strip() for n in args.bracelet.split(',') if n.strip()]
    if args.scan is not None:
        changes['ecg_scan_timeout'] = args.scan
        changes['bracelet_scan_timeout'] = args.scan
    if args.out is not None:
        changes['out_file'] = None if args.out.strip().lower() in ('', 'none') else args.out
    if args.debug:
        changes['debug'] = True

    return replace(cfg, **changes) if changes else cfg


async def run_all(cfg: Config, recorder: DataRecorder, writer: SensorWriter) -> None:
    """启动所有已配置的设备接收任务，并等待它们（正常情况不会返回）。"""
    output = PacketOutput(out_file=cfg.out_file)
    tasks = []

    if cfg.ecg_names:                                  # 蓝牙心电贴
        tasks.append(asyncio.create_task(start_receiver(
            recorder,
            cfg.ecg_names,
            cfg.ecg_scan_timeout,
            output,
            cfg.debug,
            writer=writer,
            scan_attempts=cfg.ecg_scan_attempts,
        )))

    if cfg.bracelet_names:                             # 蓝牙绿光手环（与心电共用同一个库）
        tasks.append(asyncio.create_task(start_bracelet(
            recorder,
            cfg.bracelet_names,
            cfg.bracelet_scan_timeout,
            cfg.debug,
            notify_uuid=cfg.bracelet_notify_uuid,
            start_delay=cfg.bracelet_start_delay,
            reconnect_interval=cfg.bracelet_reconnect_interval,
            rescan_interval=cfg.bracelet_rescan_interval,
            writer=writer,
            scan_attempts=cfg.bracelet_scan_attempts,
        )))

    if not tasks:
        print('未配置任何设备（config.ini 的 device_names / bracelet_names 都为空）', file=sys.stderr)
        return

    await asyncio.gather(*tasks)


def main(argv: Optional[list] = None) -> int:
    cfg = apply_overrides(Config.load(), parse_args(argv))

    if not cfg.at_least_one_device():
        print('未配置任何设备，请在 config.ini 或通过 --device / --bracelet 指定。', file=sys.stderr)
        return 1

    print('[采集] ' + cfg.describe(), flush=True)

    # 所有传感器统一写入同一个会话库（按行数/时间批量提交）
    storage = SensorDB(commit_interval=cfg.commit_interval, commit_rows=cfg.commit_rows)
    recorder = DataRecorder(storage)

    # 写库线程：BLE 通知回调只入队，解析/入库/打印/落盘都在该线程执行，
    # 避免阻塞事件循环导致 BLE 通知被整条丢弃（详见 core/writer_thread.py）
    writer = SensorWriter(recorder, flush_interval=cfg.flush_interval)
    writer.start()
    print('[存储] ' + storage.db_path, flush=True)

    try:
        asyncio.run(run_all(cfg, recorder, writer))
    except KeyboardInterrupt:
        print('\n[采集] 收到 Ctrl+C，正在收尾…', flush=True)
    finally:
        writer.stop()
        storage.close()
        print('[采集] 已停止，' + writer.stats_line(), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
