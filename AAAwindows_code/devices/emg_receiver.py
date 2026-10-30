# -*- coding: utf-8 -*-
"""表面肌电接收（**预留骨架**，默认不启动）。

接入方式沿用主工程的做法：EMG 设备通过 **LSL** 推流，`pylsl` 里按流名找到
`StreamInlet`，逐样本读取后走与心电/手环相同的「写库线程」通道。

为什么现在只是骨架：
  * `pylsl` 不在本子工程的 `requirements.txt` 里（`pip install pylsl` 才能用）；
  * 表结构还没定（主工程是 `data_emg(value BLOB)`，本项目倾向按通道存 JSON）；
  * 界面侧 `ui/pages_extra.EmgPage` 已经把占位页和接入清单写好了。

接的时候只需要：
  1. `pip install pylsl`
  2. 在 `core/db_storage.py` 的 `DataRecorder` 里加 `record_emg`
  3. 把本文件的 `start_emg` 加进 `run_monitor.BleService._tasks_to_start()` 的返回列表
  4. `config.ini` 的 `[devices] emg_enabled = true`
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


async def start_emg(recorder, stream_name: str = 'BLE_ExG5_TimeSeries',
                    writer=None, status=None, timeout: float = 5.0) -> None:
    """扫描 LSL 流并持续接收表面肌电。

    当前实现只做「能不能找到流」的探测，找到也只会打日志、不写库，
    避免半成品数据污染会话库。
    """
    if status is not None:
        status.set('emg', 'connecting', stream_name, '正在查找 LSL 流…')

    try:
        from pylsl import StreamInlet, resolve_bypred      # noqa: F401 - 预留依赖
    except ImportError as exc:
        if status is not None:
            status.set('emg', 'error', stream_name, '未安装 pylsl：pip install pylsl')
        print('[肌电] 预留未启用：{}'.format(exc), flush=True)
        return

    while True:
        try:
            streams = resolve_bypred("name='{}'".format(stream_name),
                                     minimum=1, timeout=timeout)
        except Exception as exc:                  # noqa: BLE001 - 探测失败继续重试
            if status is not None:
                status.set('emg', 'error', stream_name, str(exc))
            await asyncio.sleep(timeout)
            continue

        if not streams:
            if status is not None:
                status.set('emg', 'not_found', stream_name,
                           '未找到 LSL 流 {}，继续重试…'.format(stream_name))
            await asyncio.sleep(timeout)
            continue

        if status is not None:
            status.set('emg', 'connected', stream_name, '已找到流（采样逻辑待接入）')
        print('[肌电] 已找到 LSL 流 {}，接收逻辑待接入'.format(stream_name), flush=True)
        # TODO: inlet = StreamInlet(streams[0]) → pull_sample → writer.submit(...)
        await asyncio.sleep(3600)
