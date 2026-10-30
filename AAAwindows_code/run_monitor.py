#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时监测界面入口（`main.py` 的可视化版）。

用法：
    python run_monitor.py                              # 真实蓝牙，读 config.ini
    python run_monitor.py --device E1220511000413      # 覆盖心电设备名（多个用逗号）
    python run_monitor.py --bracelet MPPB20000008      # 覆盖手环设备名
    python run_monitor.py --replay data/20261016_172216.db --speed 2
    python run_monitor.py --no-ble                     # 只开界面，稍后在「状态」页选文件回放
    python run_monitor.py --auto-connect               # 启动即执行「设备准备」

与 main.py 的区别：
    main.py       纯采集，只写库
    run_monitor.py 同一套接收模块 + 界面。入库逻辑完全复用，界面数据靠
                   `MonitorRecorder` 在入库之后顺手推给 `LiveBuffers`，
                   接收模块不需要知道界面的存在。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from dataclasses import replace
from typing import Any, Dict, List, Optional

from core.preflight import check_dependencies

check_dependencies(need_ui=True)          # 缺 PyQt5 等依赖时给出明确指引，不再甩 traceback

from PyQt5.QtCore import QTimer

from core.config import Config
from core.db_storage import DataRecorder, SensorDB
from core.device_status import DeviceStatus
from core.writer_thread import SensorWriter
from devices.bracelet_receiver import start_bracelet
from devices.ecg_receiver import PacketOutput, start_receiver
from ui.buffers import LiveBuffers, MonitorRecorder
from ui.profile import ProfileDialog
from ui.window import MonitorWindow, create_app


class BleService(threading.Thread):
    """在后台线程里跑 bleak 的 asyncio 事件循环，界面线程保持干净。"""

    def __init__(self, cfg: Config, recorder: DataRecorder, writer: SensorWriter,
                 output: PacketOutput, status: Optional[DeviceStatus] = None,
                 overrides: Optional[Dict[str, Any]] = None):
        super().__init__(name='ble-service', daemon=True)
        self.cfg = cfg
        self.recorder = recorder
        self.writer = writer
        self.output = output
        self.status = status
        self.overrides = overrides or {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.tasks: List[asyncio.Task] = []

    def _tasks_to_start(self):
        """按「命令行覆盖 > config.ini」决定要启动哪些接收协程。"""
        cfg = self.cfg
        ecg_names = self.overrides.get('ecg_names', cfg.ecg_names)
        bracelet_names = self.overrides.get('bracelet_names', cfg.bracelet_names)

        tasks = []
        if ecg_names:
            tasks.append(start_receiver(
                self.recorder, ecg_names, cfg.ecg_scan_timeout, self.output, cfg.debug,
                writer=self.writer, status=self.status,
                scan_attempts=cfg.ecg_scan_attempts))
        if bracelet_names:
            tasks.append(start_bracelet(
                self.recorder, bracelet_names, cfg.bracelet_scan_timeout, cfg.debug,
                notify_uuid=cfg.bracelet_notify_uuid,
                start_delay=cfg.bracelet_start_delay,
                reconnect_interval=cfg.bracelet_reconnect_interval,
                rescan_interval=cfg.bracelet_rescan_interval,
                writer=self.writer, status=self.status,
                scan_attempts=cfg.bracelet_scan_attempts))
        return tasks

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            coros = self._tasks_to_start()
            if not coros:
                print('[界面] 没有配置任何设备（config.ini 的 device_names / '
                      'bracelet_names 都为空）', flush=True)
                return
            self.tasks = [self.loop.create_task(coro) for coro in coros]
            self.loop.run_until_complete(asyncio.gather(*self.tasks))
        except asyncio.CancelledError:
            pass
        except Exception as exc:                      # noqa: BLE001 - 界面不能因为蓝牙异常退出
            print('[界面] 蓝牙服务异常：{}'.format(exc), flush=True)
        finally:
            try:
                self.loop.close()
            except Exception:                         # noqa: BLE001
                pass

    def stop(self, timeout: float = 3.0) -> None:
        if self.loop is not None and self.tasks:
            def _cancel():
                for task in self.tasks:
                    task.cancel()
            try:
                self.loop.call_soon_threadsafe(_cancel)
            except RuntimeError:
                pass
        if self.is_alive():
            self.join(timeout)


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='蓝牙生理数据实时监测界面')
    parser.add_argument('--device', '-d', help='心电设备名（逗号分隔，覆盖 config.ini）')
    parser.add_argument('--bracelet', '-b', help='手环设备名（逗号分隔，覆盖 config.ini）')
    parser.add_argument('--scan', '-s', type=float, help='扫描时长（秒）')
    parser.add_argument('--replay', help='启动后直接回放这个 .db / .jsonl')
    parser.add_argument('--speed', type=float, default=1.0, help='回放倍速')
    parser.add_argument('--no-ble', action='store_true', help='不启动蓝牙，只开界面')
    parser.add_argument('--auto-connect', action='store_true',
                        help='启动后自动执行「设备准备」流程（连蓝牙 + 开摄像头）')
    parser.add_argument('--out', help='JSONL 输出路径（默认不写；传 none 表示不写）')
    parser.add_argument('--debug', action='store_true', help='打印 BLE 服务/特征表')
    parser.add_argument('--no-profile', action='store_true',
                        help='跳过登录/被试信息录入（自动跑、截图自检时用）')
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    cfg = Config.load()

    # 命令行覆盖（只影响本次运行，不回写 config.ini）
    changes = {}
    if args.scan is not None:
        changes['ecg_scan_timeout'] = args.scan
        changes['bracelet_scan_timeout'] = args.scan
    if args.debug:
        changes['debug'] = True
    if args.out is not None:
        changes['out_file'] = None if args.out.strip().lower() in ('', 'none') else args.out
    if changes:
        cfg = replace(cfg, **changes)

    overrides = {}
    if args.device is not None:
        overrides['ecg_names'] = [n.strip() for n in args.device.split(',') if n.strip()]
    if args.bracelet is not None:
        overrides['bracelet_names'] = [n.strip() for n in args.bracelet.split(',') if n.strip()]

    buffers = LiveBuffers()
    status = DeviceStatus()

    # 必须先建 QApplication，再碰任何 QWidget。
    # Qt 里"没有 QApplication 就创建 QWidget"是 qFatal → abort()，
    # Windows 上表现为进程直接崩掉、退出码 0xC0000409（STATUS_STACK_BUFFER_OVERRUN），
    # 而不是一个能看懂的 Python 异常。登录对话框也是 QWidget，所以它必须排在后面。
    app = create_app(sys.argv)

    # 再录被试信息：人物名/组别/实验时间决定会话目录 data/<组别>/<人物名>_<时间>/
    profile = None if args.no_profile else ProfileDialog.collect(cfg)
    session_subdir = profile.session_subdir() if profile else None

    storage = SensorDB(commit_interval=cfg.commit_interval, commit_rows=cfg.commit_rows,
                       session_subdir=session_subdir)
    recorder = MonitorRecorder(storage, buffers)
    writer = SensorWriter(recorder, flush_interval=cfg.flush_interval)
    writer.start()

    if profile is not None:
        print('[会话] {} / {} · {}'.format(profile.group, profile.name,
                                          profile.started_at.strftime('%Y-%m-%d %H:%M:%S')),
              flush=True)
    print('[存储] ' + storage.db_path, flush=True)

    service_holder: Dict[str, Optional[BleService]] = {'service': None}

    def start_ble() -> None:
        if args.no_ble:
            return
        current = service_holder['service']
        if current is not None and current.is_alive():
            return
        service = BleService(cfg, recorder, writer, PacketOutput(out_file=cfg.out_file),
                             status=status, overrides=overrides)
        service_holder['service'] = service
        service.start()

    def stop_ble() -> None:
        current = service_holder['service']
        if current is not None:
            current.stop()
            service_holder['service'] = None

    window = MonitorWindow(
        buffers,
        session_info={
            'db_path': storage.db_path,
            'source': '回放' if args.replay else ('仅界面' if args.no_ble else '实时蓝牙'),
            'subject': '{} / {}'.format(profile.name, profile.group) if profile else '临时会话',
            'server': '{}:{}'.format(profile.server_host, cfg.server_port) if profile
                      else '{}:{}'.format(cfg.server_host, cfg.server_port),
        },
        writer=writer,
        status=status,
        ble_start=None if args.no_ble else start_ble,
        ble_stop=None if args.no_ble else stop_ble,
    )
    window.show()

    if args.replay:
        count = window.start_replay(args.replay, args.speed)
        print('[界面] 回放 {} 个包：{}'.format(count, args.replay), flush=True)
    elif args.auto_connect and not args.no_ble:
        QTimer.singleShot(300, window.connect_devices)

    try:
        return app.exec_()
    finally:
        stop_ble()
        writer.stop()
        storage.close()


if __name__ == '__main__':
    raise SystemExit(main())
