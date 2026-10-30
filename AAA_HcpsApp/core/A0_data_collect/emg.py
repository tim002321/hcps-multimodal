# -*- coding: utf-8 -*-
"""表面肌电采集：LSL 流（不是 TCP！）。

来源：老工程 data_emg.py 的 Data_Emg_Thread。
老代码里配置读的是 [data_EMG_win] 的 Server_IP / Server_Port，但那两个值其实没用，
真正干活的是 `resolve_bypred("name='BLE_<设备名>_TimeSeries'")` —— 所以新配置
只保留 device_name（以及可覆盖的 stream_predicate）。

写库契约（A1 层负责落库）：
    data_emg.value = json.dumps({'sample': [...], 'timestamp': float, 'send_time': '...'}).encode()
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)


class EmgCollector(BaseCollector):
    """LSL 肌电采集器（pylsl 缺失时会明确报错，不影响其它采集器启动）。"""

    name = 'emg'

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None):
        super().__init__(config, sink)
        self.device_name: str = self.config.get('device_name', 'ExG5')
        self.predicate: str = self.config.get('stream_predicate') or f"name='BLE_{self.device_name}_TimeSeries'"
        self.resolve_timeout: float = float(self.config.get('resolve_timeout', 5.0))
        self.pull_timeout: float = float(self.config.get('pull_timeout', 5.0))
        self._inlet = None

    def setup(self) -> None:
        try:
            from pylsl import StreamInlet, resolve_bypred
        except ImportError as exc:                          # pragma: no cover
            raise RuntimeError('未安装 pylsl，无法采集肌电：pip install pylsl') from exc

        streams = resolve_bypred(self.predicate, minimum=1, timeout=self.resolve_timeout)
        if not streams:
            raise RuntimeError(f'未找到 LSL 流：{self.predicate}')
        self._inlet = StreamInlet(streams[0])
        log.info('[emg] 已连接 LSL 流：%s', streams[0].name())

    def run_loop(self) -> None:
        inlet = self._inlet
        if inlet is None:
            raise RuntimeError('LSL inlet 未初始化')
        while not self.should_stop():
            sample, timestamp = inlet.pull_sample(timeout=self.pull_timeout)
            if not sample:
                continue
            payload = {'sample': sample, 'timestamp': timestamp, 'send_time': now_str()}
            self.emit(payload, t_device=timestamp)

    def teardown(self) -> None:
        inlet, self._inlet = self._inlet, None
        if inlet is not None:
            try:
                inlet.close_stream()
            except Exception:                               # noqa: BLE001 - 关闭失败不影响收尾
                log.debug('[emg] 关闭 LSL inlet 失败', exc_info=True)
