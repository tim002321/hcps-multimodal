# -*- coding: utf-8 -*-
"""config.ini 的唯一读取入口。

改造前 `main.py`、`run_monitor.py`、`tools/ble_scan.py` 各自写了一遍
`configparser` 解析 + fallback，键名和默认值容易走偏（例如 `scan_attempts`
只在 run_monitor 里被读到，main.py 一直用默认值 10）。这里统一成：

    config.ini  →  Config.load()  →  Config（强类型 dataclass）

用法：
    from core.config import Config

    cfg = Config.load()                 # 默认读项目根目录的 config.ini
    cfg.ecg_names                       # ['E1220511000413', ...]
    cfg.bracelet_scan_attempts          # 10

说明：
  - 所有字段都有与原来一致的默认值，config.ini 缺项不会报错；
  - `at_least_one_device()` 把「心电和手环都没配」的判断集中到一处；
  - 路径一律基于本文件位置推导，不再依赖当前工作目录（cwd）。
"""
from __future__ import annotations

import configparser
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import List, Optional


def _app_root() -> str:
    """**可写**的根：配置文件与 data/ 都放这里。

    * 源码运行：项目根目录 `AAAwindows_code/`；
    * PyInstaller 打包后：**exe 所在目录**（不是 `_internal`）—— 这样 config.ini
      用户可以自己改、data/ 能随文件夹一起拷走备份，也不会被重新打包覆盖。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundle_root() -> str:
    """**只读**资源（models/、ui/assets）的根：打包后在 PyInstaller 的解包目录。

    源码运行时与 `_app_root()` 相同；冻结后 `__file__` 指向 `_internal/`，
    模型权重、图标这类跟着包走的只读文件必须按这个根去找。
    """
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', _app_root())
    return _app_root()


# 源码运行时两者相同；打包后 PROJECT_ROOT 是 exe 同级，BUNDLE_ROOT 是 _internal
PROJECT_ROOT = _app_root()
BUNDLE_ROOT = _bundle_root()
CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config.ini')
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')


def _split_names(raw: str) -> List[str]:
    """把 'A,B, C' 拆成 ['A', 'B', 'C']，忽略空项。"""
    return [item.strip() for item in (raw or '').split(',') if item.strip()]


def _optional(raw: str) -> Optional[str]:
    """空字符串 / none / None 一律视为「不启用」。"""
    text = (raw or '').strip()
    if not text or text.lower() in ('none', 'null'):
        return None
    return text


@dataclass(frozen=True)
class Config:
    """一套采集参数。字段名即 config.ini 的含义，不再用字典键。"""

    # ---- 心电贴 ----
    ecg_names: List[str] = field(default_factory=list)
    ecg_scan_timeout: float = 10.0
    ecg_scan_attempts: int = 10

    # ---- 绿光手环 ----
    bracelet_names: List[str] = field(default_factory=list)
    bracelet_scan_timeout: float = 10.0
    bracelet_scan_attempts: int = 10
    bracelet_notify_uuid: Optional[str] = None
    bracelet_start_delay: float = 3.0
    bracelet_reconnect_interval: float = 5.0
    bracelet_rescan_interval: float = 10.0

    # ---- 通用 ----
    debug: bool = False
    out_file: Optional[str] = None

    # ---- 总线上传（预留）----
    server_host: str = '127.0.0.1'
    server_port: int = 9000

    # ---- 预留设备 ----
    emg_enabled: bool = False
    emg_stream: str = 'BLE_ExG5_TimeSeries'
    # 眼动仪（aSeeStudio 字符串报文，UDP 一包一帧），解析见 devices/eye_protocol.py
    eye_enabled: bool = False
    eye_udp_port: int = 8003
    eye_bind: str = '0.0.0.0'           # 监听地址；只想接本机填 127.0.0.1
    eye_idle_warn: float = 10.0         # 多久没数据就提示（秒）
    eye_debug: bool = False             # 打印每帧解析结果（联调用）
    eye_use_device_time: bool = False   # 写库时间轴：False=本机时间，True=设备时间

    # ---- 存储 ----
    commit_interval: float = 0.5
    commit_rows: int = 500
    flush_interval: float = 0.5
    data_dir: str = DATA_DIR

    # ---- 界面 ----
    # 字号倍率：1.0 = 原始设计值。高 DPI 屏由 Qt 自动缩放，
    # 这里只在"还想再大一点"时叠加（1.15 / 1.3 比较常用）。
    ui_scale: float = 1.0

    # ---- 摄像头实时人脸检测 / 打哈欠检测 ----
    # 见 vision/ 包与 docs：默认关闭，勾选后才加载模型（首次约 2~5 秒）
    vision_enabled: bool = True         # 摄像头页默认启用
    # 只做人脸框选、不判哈欠：True 时只加载人脸检测器（YOLOv8n-face），
    # 不加载张嘴分类器，也不跑时序状态机；界面「疲劳状态 / 哈欠次数」显示 N/A。
    # 关掉它（false）才恢复完整的哈欠判定。
    vision_face_only: bool = True
    vision_interval: float = 0.15        # 推理间隔（秒），约 6~7fps
    vision_threshold: float = 0.5        # 张嘴概率阈值，必须用本地素材标定
    vision_yawn_min_sec: float = 1.5     # 连续张嘴多久算一次哈欠
    vision_cooldown_sec: float = 5.0     # 一次哈欠后的冷却
    vision_dip_grace: float = 0.3        # 概率瞬时跌落的容忍窗口（秒），防抖动打断判定
    vision_backend: str = 'auto'         # auto / torch / onnx
    vision_face_conf: float = 0.35       # 人脸检测置信度阈值
    vision_center_band: float = 0.25     # 只认画面中间的人：脸中心允许偏离中线多少（占比）

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> 'Config':
        """读取 ini 并返回 Config；文件不存在时全部走默认值。"""
        target = path or CONFIG_PATH
        # 打包后 exe 同级的 config.ini 可能被用户删掉 / 第一次启动还没生成：
        # 从包内模板（_internal/config.ini）复制一份出来，保证「配置可编辑」这件事成立。
        if not os.path.exists(target):
            template = os.path.join(BUNDLE_ROOT, 'config.ini')
            if (os.path.abspath(template) != os.path.abspath(target)
                    and os.path.exists(template)):
                try:
                    shutil.copyfile(template, target)
                except OSError as exc:
                    print('[配置] 复制模板 config.ini 失败：{}'.format(exc), flush=True)

        cp = configparser.ConfigParser()
        cp.read(target, encoding='utf-8')

        def get(section: str, option: str, fallback: str = '') -> str:
            return cp.get(section, option, fallback=fallback)

        return cls(
            ecg_names=_split_names(get('bluetooth', 'device_names')),
            ecg_scan_timeout=cp.getfloat('bluetooth', 'scan_timeout', fallback=10.0),
            ecg_scan_attempts=cp.getint('bluetooth', 'scan_attempts', fallback=10),

            bracelet_names=_split_names(get('bracelet', 'bracelet_names')),
            bracelet_scan_timeout=cp.getfloat('bracelet', 'scan_timeout', fallback=10.0),
            bracelet_scan_attempts=cp.getint('bracelet', 'scan_attempts', fallback=10),
            bracelet_notify_uuid=_optional(get('bracelet', 'notify_uuid')),
            bracelet_start_delay=cp.getfloat('bracelet', 'start_delay', fallback=3.0),
            bracelet_reconnect_interval=cp.getfloat('bracelet', 'reconnect_interval', fallback=5.0),
            bracelet_rescan_interval=cp.getfloat('bracelet', 'rescan_interval', fallback=10.0),

            debug=cp.getboolean('bluetooth', 'debug', fallback=False),
            out_file=_optional(get('output', 'out_file')),

            server_host=get('server', 'host') or '127.0.0.1',
            server_port=cp.getint('server', 'port', fallback=9000),

            emg_enabled=cp.getboolean('devices', 'emg_enabled', fallback=False),
            emg_stream=get('devices', 'emg_stream') or 'BLE_ExG5_TimeSeries',
            eye_enabled=cp.getboolean('devices', 'eye_enabled', fallback=False),
            eye_udp_port=cp.getint('devices', 'eye_udp_port', fallback=8003),
            eye_bind=get('devices', 'eye_bind') or '0.0.0.0',
            eye_idle_warn=cp.getfloat('devices', 'eye_idle_warn', fallback=10.0),
            eye_debug=cp.getboolean('devices', 'eye_debug', fallback=False),
            eye_use_device_time=cp.getboolean('devices', 'eye_use_device_time', fallback=False),

            commit_interval=cp.getfloat('storage', 'commit_interval', fallback=0.5),
            commit_rows=cp.getint('storage', 'commit_rows', fallback=500),
            flush_interval=cp.getfloat('storage', 'flush_interval', fallback=0.5),

            ui_scale=cp.getfloat('ui', 'scale', fallback=1.0),

            vision_enabled=cp.getboolean('vision', 'enabled', fallback=True),
            vision_face_only=cp.getboolean('vision', 'face_only', fallback=True),
            vision_interval=cp.getfloat('vision', 'interval', fallback=0.15),
            vision_threshold=cp.getfloat('vision', 'threshold', fallback=0.5),
            vision_yawn_min_sec=cp.getfloat('vision', 'yawn_min_sec', fallback=1.5),
            vision_cooldown_sec=cp.getfloat('vision', 'cooldown_sec', fallback=5.0),
            vision_dip_grace=cp.getfloat('vision', 'dip_grace', fallback=0.3),
            vision_backend=get('vision', 'backend') or 'auto',
            vision_face_conf=cp.getfloat('vision', 'face_conf', fallback=0.35),
            vision_center_band=cp.getfloat('vision', 'center_band', fallback=0.25),
        )

    # ------------------------------------------------------------------
    def at_least_one_device(self) -> bool:
        """心电贴和手环至少配了一个（都没配就没必要启动采集）。"""
        return bool(self.ecg_names or self.bracelet_names)

    def describe(self) -> str:
        """一行摘要，启动时打印，便于确认读到的确实是这份配置。"""
        extra = []
        if self.emg_enabled:
            extra.append('肌电')
        if self.eye_enabled:
            extra.append('眼动')
        return (
            '心电设备 {} 个 / 手环设备 {} 个 / 扫描 {:.0f}s×{} 次 / 写文件 {} / 总线 {}:{}{}'.format(
                len(self.ecg_names), len(self.bracelet_names),
                self.ecg_scan_timeout, self.ecg_scan_attempts,
                self.out_file or '不写',
                self.server_host, self.server_port,
                (' / 预留设备 ' + '+'.join(extra)) if extra else '',
            )
        )
