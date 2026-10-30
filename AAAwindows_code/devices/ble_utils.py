# -*- coding: utf-8 -*-
"""BLE 公共工具：两个接收模块原来各写了一份的服务表打印与按名扫描。

这里的函数只做「查」和「打印」，不持有连接、不碰状态表，方便单独复用与测试。
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from bleak import BleakScanner

# 一个匹配结果：(BLEDevice, rssi)；rssi 在广播里没有时为 None
ScanHit = Tuple[object, object]


def iter_characteristics(client):
    """遍历 client.services 下的 (service, characteristic)，屏蔽 bleak 的两层嵌套。"""
    for service in client.services:
        for characteristic in service.characteristics:
            yield service, characteristic


def dump_services(client, tag: str = '') -> None:
    """打印设备的服务 / 特征表（排查 handle 与 UUID 对应关系时用）。

    改造前心电和手环各有一份几乎相同的实现，只有前缀不同，这里合二为一。
    """
    prefix = '[{}] '.format(tag) if tag else ''
    print('-' * 60, flush=True)
    print(prefix + '服务 / 特征列表（handle -> uuid, 属性）:', flush=True)
    for service in client.services:
        print('  服务 {} [{}]'.format(service.description, service.uuid), flush=True)
        for characteristic in service.characteristics:
            print(
                '    0x{:04X}  {}  {}'.format(
                    characteristic.handle,
                    characteristic.uuid,
                    ','.join(characteristic.properties),
                ),
                flush=True,
            )
    print('-' * 60, flush=True)


def find_characteristics(client, keywords: Iterable[str]) -> List[object]:
    """按属性关键字找特征，例如 `find_characteristics(client, ('notify',))`。"""
    wanted = {keyword.lower() for keyword in keywords}
    found = []
    for _service, characteristic in iter_characteristics(client):
        properties = {prop.lower() for prop in characteristic.properties}
        if properties & wanted:
            found.append(characteristic)
    return found


async def scan_for_names(
    device_names: Sequence[str],
    timeout: float,
    return_adv: bool = True,
) -> List[ScanHit]:
    """扫描一次，返回广播名称命中 `device_names` 的设备。

    名称取值顺序与原来一致：`dev.name` 优先，其次 `adv.local_name`。
    bleak 2.x/3.x 都需要 `return_adv=True` 才能拿到 rssi 与 local_name，
    旧写法 `for dev, adv in found.values()` 依赖这一点，这里保持不回退。
    """
    wanted = {name for name in (device_names or []) if name}
    if not wanted:
        return []

    found = await BleakScanner.discover(timeout=timeout, return_adv=return_adv)

    hits: List[ScanHit] = []
    for device, adv in found.values():
        name = device.name or getattr(adv, 'local_name', None) or ''
        if name in wanted:
            hits.append((device, getattr(adv, 'rssi', None)))
    return hits
