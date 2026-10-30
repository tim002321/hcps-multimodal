# -*- coding: utf-8 -*-
"""眼动仪接收（**预留骨架**，默认不启动）。

眼动仪（SmartEye）是 UDP 推流，报文是「4 字节 id + 2 字节 type + 2 字节 len +
若干子包」的自定义结构，主工程 `data_udp.py` 里已有完整解包实现（`decode_eye` /
`Smarteye_decode`），可以直接搬过来按本项目风格拆成：

    devices/eye_protocol.py   纯解包（无 socket 依赖，可离线用报文片段测）
    devices/eye_receiver.py   UDP 收包 → 解包 → recorder.record_eye()

为什么现在只是骨架：
  * 需要的字段（瞳孔直径、眨眼、注视点）还没确认要做哪些指标；
  * 界面侧 `ui/pages_extra.EyePage` 已写好占位页与接入清单；
  * `config.ini` 的 `[devices] eye_udp_port` 已经预留了端口。

接的时候只需要：
  1. 把主工程 `data_udp.py` 的 `decode_eye` + `Smarteye_decode` 拷进 `eye_protocol.py`
  2. 本文件的 `start_eye` 里起一个 UDP 收包循环（`socket.SOCK_DGRAM`）
  3. `core/db_storage.py` 加 `record_eye`，表名建议 `eye_data(time, value)`
  4. `config.ini` 的 `[devices] eye_enabled = true`
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

# 眼动仪常被用到的字段（与主工程 eye_namedict 对齐），接入后按需取子集
FIELDS_OF_INTEREST = (
    'PupilDiameter',        # 瞳孔直径
    'Blink',                # 眨眼标记
    'EstimatedLeftGazeHeading',
    'EstimatedLeftGazePitch',
    'EstimatedRightGazeHeading',
    'EstimatedRightGazePitch',
)


async def start_eye(recorder, port: int = 8003, bind_host: str = '0.0.0.0',
                    writer=None, status=None) -> None:
    """监听眼动仪 UDP 端口。

    当前只做「端口能不能绑上」的探测，不解析、不写库。
    """
    loop = asyncio.get_running_loop()
    sock = None
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, port))
        sock.setblocking(False)
    except OSError as exc:
        if status is not None:
            status.set('eye', 'error', '', 'UDP {} 绑定失败：{}'.format(port, exc))
        print('[眼动] 预留未启用：{}'.format(exc), flush=True)
        if sock is not None:
            sock.close()
        return

    if status is not None:
        status.set('eye', 'connected', '', 'UDP {} 已监听（解包逻辑待接入）'.format(port))
    print('[眼动] UDP {} 已监听，解包逻辑待接入'.format(port), flush=True)

    try:
        while True:
            data, addr = await loop.sock_recvfrom(sock, 65535)
            # TODO: decoded = Smarteye_decode(data) → writer.submit(...)
            _ = (data, addr)
    finally:
        sock.close()
