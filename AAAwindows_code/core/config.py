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
from dataclasses import dataclass, field
from typing import List, Optional

# 项目根目录：AAAwindows_code/（本文件在 core/ 下）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    eye_enabled: bool = False
    eye_udp_port: int = 8003

    # ---- 存储 ----
    commit_interval: float = 0.5
    commit_rows: int = 500
    flush_interval: float = 0.5
    data_dir: str = DATA_DIR

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> 'Config':
        """读取 ini 并返回 Config；文件不存在时全部走默认值。"""
        cp = configparser.ConfigParser()
        cp.read(path or CONFIG_PATH, encoding='utf-8')

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

            commit_interval=cp.getfloat('storage', 'commit_interval', fallback=0.5),
            commit_rows=cp.getint('storage', 'commit_rows', fallback=500),
            flush_interval=cp.getfloat('storage', 'flush_interval', fallback=0.5),
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
