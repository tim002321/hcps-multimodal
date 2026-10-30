# -*- coding: utf-8 -*-
"""A0 采集层：设备 -> DataPacket。

* base.py     基类：DataPacket / 生命周期 / 重连 / 统计
* ecg/eye/emg/grip/video   各设备采集器
* sim.py      模拟数据源（无硬件开发与回归测试用）
* manager.py  采集总控：启停、分发、状态
"""
from core.A0_data_collect.base import (BaseCollector, CollectorState, CollectorStats, DataPacket,  # noqa: F401
                                       Sink, SocketServerCollector, now_str)
from core.A0_data_collect.camera import CameraCollector      # noqa: F401
from core.A0_data_collect.ecg import EcgCollector            # noqa: F401
from core.A0_data_collect.eye import EyeCollector            # noqa: F401
from core.A0_data_collect.emg import EmgCollector            # noqa: F401
from core.A0_data_collect.grip import GripCollector          # noqa: F401
from core.A0_data_collect.video import VideoCollector        # noqa: F401
from core.A0_data_collect.sim import SimCollector            # noqa: F401
from core.A0_data_collect.manager import CollectorManager    # noqa: F401

__all__ = ['BaseCollector', 'CollectorState', 'CollectorStats', 'DataPacket', 'Sink',
           'SocketServerCollector', 'now_str', 'CameraCollector', 'EcgCollector', 'EyeCollector',
           'EmgCollector', 'GripCollector', 'VideoCollector', 'SimCollector', 'CollectorManager']
