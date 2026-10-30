# -*- coding: utf-8 -*-
"""设备连接状态登记表：BLE 接收线程写、界面线程读。

状态取值：
    idle        还没开始
    scanning    正在扫描
    connecting  扫到了，正在连接
    connected   已连接（开始收数据）
    lost        掉线（会自动重连）
    not_found   扫描没找到
    error       连接/打开失败

只做一件小事：加锁的字典 + 时间戳。界面每秒取一次快照刷新「设备准备」页，
不在 BLE 线程里碰任何 Qt 对象（跨线程操作控件会崩，之前踩过）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

STATE_TEXT = {
    'idle': '未开始',
    'scanning': '扫描中',
    'connecting': '连接中',
    'connected': '已连接',
    'lost': '已掉线',
    'not_found': '未找到',
    'error': '连接失败',
}

DEVICE_TEXT = {
    'ecg': '心电贴',
    'bracelet': '绿光手环',
    'camera': 'USB 摄像头',
    'emg': '表面肌电',
    'eye': '眼动仪',
}


class DeviceStatus:
    """线程安全的设备状态表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: Dict[str, Dict[str, Any]] = {
            key: {'key': key, 'title': title, 'state': 'idle', 'name': '',
                  'detail': '未开始', 'updated_at': 0.0}
            for key, title in DEVICE_TEXT.items()
        }

    def set(self, key: str, state: str, name: str = '', detail: str = '') -> None:
        with self._lock:
            item = self._devices.setdefault(
                key, {'key': key, 'title': DEVICE_TEXT.get(key, key)})
            item.update({
                'state': state,
                'name': name or item.get('name', ''),
                'detail': detail or STATE_TEXT.get(state, state),
                'updated_at': time.time(),
            })

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._devices.get(key, {}))

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._devices.items()}

    def all_connected(self, keys=('ecg', 'bracelet', 'camera')) -> bool:
        with self._lock:
            return all(self._devices.get(key, {}).get('state') == 'connected' for key in keys)

    def any_connected(self) -> bool:
        with self._lock:
            return any(value.get('state') == 'connected' for value in self._devices.values())

    def reset(self) -> None:
        with self._lock:
            for value in self._devices.values():
                value.update({'state': 'idle', 'detail': '未开始', 'updated_at': 0.0})
