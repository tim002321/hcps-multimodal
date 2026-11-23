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


def _preload_vision_runtime() -> None:
    """在任何其他第三方包之前，把视觉推理的原生依赖加载好。

    这不是提速，是躲一个必崩的坑：torch / ultralytics / onnxruntime 各自带了
    一份 OpenMP、protobuf、abseil 原生库，如果 OpenCV 或 Qt 先把进程的 DLL
    状态立起来、之后才导入它们，Windows 下会直接段错误（实测退出码 139，
    Python 层抓不到异常，界面直接消失，连 traceback 都没有）。

    实测规律：只要这三者在 cv2 / PyQt5 之前导入就稳定；放在后面，
    崩溃点固定在 `onnxruntime/capi/_pybind_state.py` 加载 DLL 的那一行。
    所以这段必须放在文件最前面，`core.preflight`（会 import cv2/PyQt5）之前。


    未安装这些包时静默跳过 —— 打哈欠检测是可选功能，缺了不影响采集。
    """
    try:
        from vision.yawn_monitor import _preload_heavy_modules
        _preload_heavy_modules()
    except Exception:                                     # noqa: BLE001 - 可选功能，不阻断启动
        pass


_preload_vision_runtime()

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
from ui import theme
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
    parser.add_argument('--ui-scale', type=float, default=None,
                        help='界面字号倍率（1.0 = 默认；觉得字还小就试 1.15 / 1.3）。'
                             '也可用环境变量 HCP_UI_SCALE')
    parser.add_argument('--selftest', action='store_true',
                        help='部署自检：依赖/模型/数据目录/融合管线逐项检查，'
                             '结果写到程序目录的 selftest.log 后退出（不开界面）')
    return parser.parse_args(argv)


def run_selftest() -> int:
    """部署自检：换了电脑、换了目录之后先跑这个。

    检查项：路径布局 → 配置 → 数据目录可写 → Qt → 模型文件 → 推理后端 → 融合管线。
    结果同时打印和写到程序目录的 selftest.log（打包成窗口程序后看不到控制台）。
    """
    import platform
    import traceback

    from core import paths

    lines: list = []

    def log(text: str = '') -> None:
        print(text, flush=True)
        lines.append(text)

    ok = True
    log('=' * 66)
    log('HCPS 采集程序 · 部署自检')
    log('=' * 66)
    log('时间      : {}'.format(__import__('datetime').datetime.now()
                                .strftime('%Y-%m-%d %H:%M:%S')))
    log('Python    : {}'.format(sys.version.split()[0]))
    log('平台      : {} / {}'.format(platform.system(), platform.machine()))
    log('打包运行  : {}'.format(paths.FROZEN))
    log('程序目录  : {}'.format(paths.APP_DIR))
    log('资源目录  : {}'.format(paths.BUNDLE_DIR))

    def check(name: str, func):
        nonlocal ok
        try:
            detail = func()
            log('[ OK ] {}{}'.format(name, '' if detail is None else '：{}'.format(detail)))
        except Exception as exc:                       # noqa: BLE001
            ok = False
            log('[FAIL] {}：{}'.format(name, exc))
            for line in traceback.format_exc().strip().splitlines()[-3:]:
                log('       ' + line)

    def _config():
        from core.config import Config
        cfg = Config.load()
        return '心电 {} 个 / 手环 {} 个 / UI 倍率 {}'.format(
            len(cfg.ecg_names), len(cfg.bracelet_names), cfg.ui_scale)

    def _data_dir():
        import sqlite3
        from core.config import DATA_DIR
        probe = os.path.join(DATA_DIR, '_selftest.db')
        con = sqlite3.connect(probe)
        con.execute('CREATE TABLE IF NOT EXISTS t (v TEXT)')
        con.execute("INSERT INTO t VALUES ('ok')")
        con.commit()
        value = con.execute('SELECT v FROM t').fetchone()[0]
        con.close()
        os.remove(probe)
        return '{}（可写，sqlite {}）'.format(DATA_DIR, value)

    def _qt():
        from PyQt5.QtCore import QT_VERSION_STR
        from PyQt5.QtWidgets import QApplication
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        app = QApplication.instance() or QApplication([])
        name = app.platformName()
        return 'Qt {} / 平台插件 {}'.format(QT_VERSION_STR, name)

    def _models():
        from core.config import Config
        from vision.face_detector import MODELS_DIR, TORCH_MODEL, ONNX_MODEL
        found = [os.path.basename(p) for p in (TORCH_MODEL, ONNX_MODEL)
                 if os.path.exists(p)]
        cfg = Config.load()
        backend = (getattr(cfg, 'vision_backend', 'auto') or 'auto').lower()
        # ort 后端只认 ONNX；torch 只认 .pt。按配置检查对应文件，别一刀切要求 .pt。
        if backend == 'ort' and not os.path.exists(ONNX_MODEL):
            raise FileNotFoundError('缺少 {}（运行 fetch_models.py --with-onnx）'
                                    .format(os.path.basename(ONNX_MODEL)))
        if not found:
            raise FileNotFoundError('models 目录里没有任何人脸模型')
        return '{} 里有 {}'.format(MODELS_DIR, '、'.join(found))

    def _vision_config():
        from core.config import Config
        cfg = Config.load()
        return '后端 {} / 设备 {} / providers {}'.format(
            getattr(cfg, 'vision_backend', 'auto'),
            getattr(cfg, 'vision_device', 'auto'),
            ','.join(cfg.vision_providers()) if hasattr(cfg, 'vision_providers') else '-')

    def _ort_providers():
        import onnxruntime as ort
        available = ort.get_available_providers()
        return 'onnxruntime {} · 可用 {}'.format(ort.__version__, ', '.join(available))

    def _face_backend():
        import numpy as np
        from core.config import Config
        cfg = Config.load()
        from vision.face_detector import build_detector
        detector = build_detector(getattr(cfg, 'vision_face_detector', 'scrfd'),
                                  conf=getattr(cfg, 'vision_face_conf', 0.35),
                                  providers=cfg.vision_providers(),
                                  gpu_fallback=bool(getattr(cfg, 'vision_gpu_fallback', True)),
                                  backend=getattr(cfg, 'vision_backend', 'auto'))
        faces = detector.detect(np.zeros((240, 320, 3), dtype=np.uint8))
        return '{} · {} · 空帧检出 {} 张'.format(
            detector.description, detector.device_label, len(faces))

    def _keypoints():
        """确认关键点能解出来，且门控四条分支都通。

        不依赖真实摄像头：用构造的 FaceBox + 关键点跑门控；
        关键点解析则看模型输出维度（row[5:20] 必须是 5 组 (置信,x,y)）。
        """
        from vision.face_detector import FaceBox
        from vision.pose_gate import gate_face
        import numpy as np

        kp = np.array([[0.9, 100, 100], [0.9, 140, 100], [0.9, 120, 120],
                       [0.88, 105, 140], [0.85, 135, 140]], dtype=float)
        ok = gate_face(FaceBox(80, 80, 160, 160, 0.9, keypoints=kp), (480, 640))
        low = kp.copy()
        low[3:, 0] = 0.2
        miss = gate_face(FaceBox(80, 80, 160, 160, 0.9, keypoints=low), (480, 640))
        down = kp.copy()
        down[3:, 2] = 158
        head = gate_face(FaceBox(80, 80, 160, 160, 0.9, keypoints=down), (480, 640))
        none = gate_face(FaceBox(80, 80, 160, 160, 0.9), (480, 640))
        if not ok.visible or ok.source != 'mouth':
            raise RuntimeError('正常关键点没走嘴部裁剪：{}'.format(ok))
        if miss.visible or miss.reason != 'low_conf':
            raise RuntimeError('低置信分支不对：{}'.format(miss))
        if head.visible or head.reason != 'head_down':
            raise RuntimeError('低头分支不对：{}'.format(head))
        if not none.visible or none.reason != 'no_keypoints':
            raise RuntimeError('无关键点应退回整脸：{}'.format(none))
        return '四条分支通过（嘴部裁剪 {}×{}）'.format(ok.roi[2] - ok.roi[0],
                                                     ok.roi[3] - ok.roi[1])

    def _yawn_backend():
        import numpy as np
        from core.config import Config
        cfg = Config.load()
        from vision.mouth_state import MouthClassifier, DEFAULT_MODEL
        clf = MouthClassifier(providers=cfg.vision_providers()).load()
        prob = clf.predict(np.zeros((120, 100, 3), dtype=np.uint8))
        return '{} · {} · 空帧概率 {:.3f}'.format(
            os.path.basename(DEFAULT_MODEL), clf.engine, prob)

    def _fusion():
        from analysis import ppg_fusion
        available, note = ppg_fusion.available()
        if not available:
            raise RuntimeError(note)
        return note

    def _ble():
        import bleak
        from bleak.backends.winrt.client import BleakClientWinRT  # noqa: F401
        return 'bleak {} · winrt 后端已加载'.format(getattr(bleak, '__version__', '?'))

    def _peripherals():
        import serial
        import pylsl
        return 'pyserial {} / pylsl {}'.format(
            getattr(serial, '__version__', '?'), getattr(pylsl, '__version__', '?'))

    def _camera():
        import cv2
        count = 0
        for index in range(3):
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if cap.isOpened():
                count += 1
            cap.release()
        return '检测到 {} 个可用摄像头（没插相机时 0 是正常的）'.format(count)

    check('配置文件', _config)
    check('数据目录', _data_dir)
    check('Qt 平台插件', _qt)
    check('模型文件', _models)
    check('视觉配置', _vision_config)
    check('onnxruntime', _ort_providers)
    check('人脸检测后端', _face_backend)
    check('关键点 / 姿态门控', _keypoints)
    check('哈欠模型后端', _yawn_backend)
    check('四通道融合管线', _fusion)
    check('蓝牙后端', _ble)
    check('串口 / LSL', _peripherals)
    check('摄像头', _camera)

    log('')
    log('结论：{}'.format('全部通过，可以现场采集' if ok else '有检查项未通过，见上面 [FAIL]'))
    try:
        with open(os.path.join(paths.APP_DIR, 'selftest.log'), 'w',
                  encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n')
    except OSError:
        pass
    return 0 if ok else 1


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return run_selftest()
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

    # 心率单独用一条长缓冲（默认 30s）：波形曲线仍用 12s，两者互不影响
    buffers = LiveBuffers(ecg_hr_seconds=float(getattr(cfg, 'hr_window_sec', 30.0)))
    status = DeviceStatus()

    # 字号倍率：必须在建 QApplication / 任何对话框之前定下来（theme.QSS 会被重建）。
    # 优先级：命令行 --ui-scale > 环境变量 HCP_UI_SCALE > config.ini [ui] scale
    scale = args.ui_scale
    if scale is None and os.environ.get('HCP_UI_SCALE'):
        try:
            scale = float(os.environ['HCP_UI_SCALE'])
        except ValueError:
            print('[界面] 环境变量 HCP_UI_SCALE 不是数字，已忽略', flush=True)
    if scale is None:
        scale = cfg.ui_scale
    if scale and abs(scale - 1.0) > 1e-6:
        print('[界面] 字号倍率 {}'.format(theme.set_scale(scale)), flush=True)

    # 必须先建 QApplication，再碰任何 QWidget。
    # Qt 里"没有 QApplication 就创建 QWidget"是 qFatal → abort()，
    # Windows 上表现为进程直接崩掉、退出码 0xC0000409（STATUS_STACK_BUFFER_OVERRUN），
    # 而不是一个能看懂的 Python 异常。登录对话框也是 QWidget，所以它必须排在后面。
    app = create_app(sys.argv)

    # 再录被试信息：人物名/组别/实验时间决定会话目录 data/<组别>/<人物名>_<时间>/
    profile = None if args.no_profile else ProfileDialog.collect(cfg)
    # 启动时选了「直接查看历史」：不连蓝牙、不建会话目录，直接跳回放页。
    review_only = bool(profile and profile.reviewing and not args.replay)
    session_subdir = profile.session_subdir() if (profile and not review_only) else None

    storage = SensorDB(commit_interval=cfg.commit_interval, commit_rows=cfg.commit_rows,
                       session_subdir=session_subdir)
    recorder = MonitorRecorder(storage, buffers)
    writer = SensorWriter(recorder, flush_interval=cfg.flush_interval)
    writer.start()

    if profile is not None:
        print('[会话] {} / {} · {}'.format(profile.name, profile.task,
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
            'source': ('回看历史' if review_only else
                       ('回放' if args.replay else ('仅界面' if args.no_ble else '实时蓝牙'))),
            'subject': '{} / {}'.format(profile.name, profile.task) if profile else '临时会话',
            'server': '{}:{}'.format(profile.server_host, cfg.server_port) if profile
                      else '{}:{}'.format(cfg.server_host, cfg.server_port),
            # 启动时勾的测量工具决定注册哪些页面；没勾就不出现在导航里
            'devices': tuple(profile.devices) if profile else ('ecg', 'bracelet', 'camera'),
            'review_only': review_only,
            # 'ground' 地面（本机）| 'host' 上机（上位机），登录界面选的
            'host_mode': profile.host_mode if profile else 'ground',
        },
        writer=writer,
        status=status,
        ble_start=None if args.no_ble else start_ble,
        ble_stop=None if args.no_ble else stop_ble,
        config=cfg,
    )
    window.show()

    if review_only:
        window.enter_review_mode()
        print('[界面] 直接查看历史：已跳到「回放与选段」，不会新建会话库', flush=True)
    elif args.replay:
        # 命令行直接回放：先把流程推进到第②段，否则文件在播、界面却停在采集段
        window.mark_stage_done(0)
        window.goto_stage(1, force=True)
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
