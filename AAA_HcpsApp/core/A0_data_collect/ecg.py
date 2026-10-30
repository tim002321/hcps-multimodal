# -*- coding: utf-8 -*-
"""心电采集：TCP 服务端，采集卡主动推 JSON 帧过来。

来源：老工程 data_ecg.py 的 Data_Ecg_Thread + ReceiveHandler.handle。
协议解析逻辑保持不变，只把「写库 + 转发 UDP 8008」换成 emit(DataPacket)。

老代码的报文形态（采集卡 -> 本机）：
    ["type:ble", "{'ECG': {'ECG': [12, 13, ...]}}", ...]
    偶数位是标签（老代码把它们拼成字符串当 Mac_device 存库），奇数位是负载。

写库契约（A1 层负责落库）：
    data_ecg.value      = json.dumps({'ECG': [...], 'send_time': '...'})
    data_ecg.Mac_device = str(报文里的偶数位标签)  # 与老库保持一致
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Any, Dict, List, Optional

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)


class EcgCollector(BaseCollector):
    """心电 TCP 服务端采集器。"""

    name = 'ecg'
    recv_size = 8192

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None):
        super().__init__(config, sink)
        self.bind: str = self.config.get('bind', '0.0.0.0')
        self.port: int = int(self.config.get('port', 8001))
        self._server: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._io_lock = threading.Lock()

    # ------------------------------------------------------------- 生命周期
    def setup(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind, self.port))
        server.listen(5)
        server.settimeout(1.0)                 # 让 run_loop 能周期性检查停止标志
        with self._io_lock:
            self._server = server
        log.info('[ecg] TCP 服务端已监听 %s:%s', self.bind, self.port)

    def run_loop(self) -> None:
        server = self._server
        if server is None:
            raise RuntimeError('server 未初始化')
        while not self.should_stop():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            log.info('[ecg] 采集卡已连接：%s', addr)
            with self._io_lock:
                self._client = conn
            try:
                self._serve(conn)
            finally:
                with self._io_lock:
                    self._client = None
                try:
                    conn.close()
                except OSError:
                    pass
                log.info('[ecg] 采集卡断开：%s', addr)

    def teardown(self) -> None:
        with self._io_lock:
            client, self._client = self._client, None
            server, self._server = self._server, None
        for sock in (client, server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def on_stop(self) -> None:
        # 关掉 socket 才能把阻塞中的 accept/recv 拉出来
        self.teardown()

    # ------------------------------------------------------------- 收数据
    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        while not self.should_stop():
            try:
                msg = conn.recv(self.recv_size)
            except socket.timeout:
                continue
            except (ConnectionAbortedError, ConnectionResetError, OSError) as exc:
                log.info('[ecg] 连接中断：%s', exc)
                return
            if not msg:
                return
            try:
                self.handle_message(msg)
            except Exception as exc:                       # 单帧解析失败不拖垮整条链路
                log.warning('[ecg] 报文解析失败：%s', exc)

    def handle_message(self, msg: bytes) -> None:
        """解析一帧并 emit。逻辑对齐老 data_ecg.py 的 ReceiveHandler.handle。"""
        data = json.loads(msg.decode('utf-8'))
        if not isinstance(data, list) or len(data) < 2:
            return
        if data[0] == 'type:serial':                       # 老代码这里什么都不做
            return

        ble = self._parse_payload(data[1])
        ecg = self._extract_ecg(ble)
        payload = {'ECG': ecg, 'send_time': now_str()}
        self.emit(payload, raw=msg, mac=str(data[::2]))

    @staticmethod
    def _parse_payload(raw: Any) -> Any:
        """老协议里负载是 Python repr（'{\\'ECG\\': ...}'），新协议可能直接是 JSON。"""
        if not isinstance(raw, str):
            return raw
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return eval(raw)                               # noqa: S307 - 兼容历史协议格式

    @staticmethod
    def _extract_ecg(ble: Any) -> List[float]:
        """从 {'ECG': {'ECG': [...]}} 里取波形；取不到就给空列表（与老代码一致）。"""
        if not isinstance(ble, dict):
            return []
        ecg = ble.get('ECG', ble)
        if isinstance(ecg, dict):
            ecg = ecg.get('ECG', [])
        return ecg if isinstance(ecg, list) else []
