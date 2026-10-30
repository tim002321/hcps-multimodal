# -*- coding: utf-8 -*-
"""采集总控：按配置建采集器、统一启停、把数据分发给各订阅者。

替代老工程的 app.Main_Thread + running_ctrl.py：
  * 老：Main_Thread 按 start_list 手工 new 一堆线程，用 12 个全局 Event 当开关。
  * 新：Manager 按配置建采集器；采集器把 DataPacket 投进内部队列，
    一个分发线程再喂给所有 sink（存储、界面曲线、推理……），互不阻塞。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, Iterable, List, Optional

from core.A0_data_collect.base import BaseCollector, DataPacket, Sink
from core.A0_data_collect.camera import CameraCollector
from core.A0_data_collect.ecg import EcgCollector
from core.A0_data_collect.emg import EmgCollector
from core.A0_data_collect.eye import EyeCollector
from core.A0_data_collect.grip import GripCollector
from core.A0_data_collect.sim import SimCollector
from core.A0_data_collect.video import VideoCollector

log = logging.getLogger(__name__)

# 已迁移的采集器；配置文件里出现但这里没有的名字，会以「未迁移」跳过
REGISTRY: Dict[str, type] = {
    'ecg': EcgCollector,
    'eye': EyeCollector,
    'emg': EmgCollector,
    'grip': GripCollector,
    'video': VideoCollector,
    'camera': CameraCollector,
    'sim_ecg': SimCollector,
    'sim_eye': SimCollector,
    'sim_grip': SimCollector,
}


class CollectorManager:
    """采集器容器 + 数据分发。"""

    def __init__(self, queue_size: int = 20000):
        self._collectors: Dict[str, BaseCollector] = {}
        self._sinks: List[Sink] = []
        self._queue: 'queue.Queue[Optional[DataPacket]]' = queue.Queue(maxsize=queue_size)
        self._dispatcher: Optional[threading.Thread] = None
        self._running = False
        self.dropped_packets = 0

    # ------------------------------------------------------------- 注册
    def register(self, collector: BaseCollector) -> BaseCollector:
        collector.set_sink(self._on_packet)
        self._collectors[collector.name] = collector
        return collector

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    @property
    def collectors(self) -> Dict[str, BaseCollector]:
        return dict(self._collectors)

    @classmethod
    def from_config(cls, config: Dict[str, Any], use_sim: bool = False,
                    session_dir: Optional[str] = None) -> 'CollectorManager':
        """按 config['collectors'] 建采集器。

        use_sim=True 时忽略真实设备，只挂模拟源；
        session_dir 会注入给需要落盘的采集器（比如 video）。
        """
        manager = cls()
        if use_sim:
            manager.register(SimCollector({'kind': 'ecg', 'name': 'sim_ecg', 'rate_hz': 3}))
            manager.register(SimCollector({'kind': 'eye', 'name': 'sim_eye', 'rate_hz': 20}))
            return manager

        for name, cfg in (config.get('collectors') or {}).items():
            if not cfg or not cfg.get('enabled'):
                continue
            klass = REGISTRY.get(name)
            if klass is None:
                log.warning('采集器 %s 尚未迁移到新工程，已跳过（见 README 迁移清单）', name)
                continue
            if session_dir and 'session_dir' not in cfg:
                cfg = {**cfg, 'session_dir': session_dir}
            manager.register(klass(cfg, name=name) if name.startswith('sim') else klass(cfg))
        return manager

    # ------------------------------------------------------------- 生命周期
    def start(self, names: Optional[Iterable[str]] = None) -> None:
        targets = [self._collectors[n] for n in names] if names else list(self._collectors.values())
        if not targets:
            log.warning('没有可启动的采集器')
            return
        self._running = True
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name='collect-dispatcher', daemon=True)
        self._dispatcher.start()
        for collector in targets:
            collector.start()
        log.info('已启动采集器：%s', ', '.join(c.name for c in targets))

    def stop(self, timeout: float = 5.0) -> None:
        self._running = False
        for collector in self._collectors.values():
            collector.stop(timeout)
        if self._dispatcher is not None:
            self._queue.put(None)                      # 哨兵，让分发线程退出
            self._dispatcher.join(timeout)
            self._dispatcher = None
        log.info('采集已全部停止，丢弃包数：%d', self.dropped_packets)

    # ------------------------------------------------------------- 分发
    def _on_packet(self, packet: DataPacket) -> None:
        """采集线程 -> 内部队列（不阻塞采集线程）。"""
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            self.dropped_packets += 1
            try:                                        # 丢最旧的，保住最新数据
                self._queue.get_nowait()
                self._queue.put_nowait(packet)
            except queue.Empty:
                pass

    def _dispatch_loop(self) -> None:
        while True:
            packet = self._queue.get()
            if packet is None:
                return
            for sink in self._sinks:
                try:
                    sink(packet)
                except Exception:
                    log.exception('sink 处理 %s 数据失败', packet.device)

    # ------------------------------------------------------------- 状态
    def status(self) -> Dict[str, Any]:
        return {
            name: {'state': c.state.value, **c.stats.as_dict()}
            for name, c in self._collectors.items()
        }
