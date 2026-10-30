# -*- coding: utf-8 -*-
"""A0 采集层基类：统一数据包、生命周期、重连、统计。

和老工程的区别
--------------
老：每个设备一个 threading.Thread，自己读 config.ini，自己写数据库，
    自己往 127.0.0.1:8008 转发给界面，开关散落在 running_ctrl.py 的全局 Event 里。
新：子类只实现 setup / run_loop / teardown 三件事；线程启停、重连退避、
    统计、停止信号都在基类；数据统一包成 DataPacket 交给 sink，
    由上层决定落库 / 画图 / 送推理。

不要改 payload 的字段结构——它和 historian 数据库、分析代码是绑定的契约，
详见 README「兼容契约」。
"""
from __future__ import annotations

import abc
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


def now_str() -> str:
    """本机时间字符串，格式与老库 created_date / send_time 一致。"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')


class CollectorState(str, Enum):
    IDLE = 'idle'
    STARTING = 'starting'
    RUNNING = 'running'
    RECONNECTING = 'reconnecting'
    ERROR = 'error'
    STOPPING = 'stopping'
    STOPPED = 'stopped'


@dataclass
class DataPacket:
    """一条采集数据。payload 的字段名要保持和历史一致。"""

    device: str                      # 'ecg' / 'eye' / 'emg' / 'grip' / 'alert' ...
    payload: Dict[str, Any]          # 结构化数据（协议解出来的字段）
    t_recv: str = field(default_factory=now_str)   # 本机接收时间
    t_device: Optional[float] = None               # 设备时间戳，协议里有就填
    raw: Optional[bytes] = None                    # 原始字节，排查问题用
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectorStats:
    packets: int = 0
    errors: int = 0
    reconnects: int = 0
    last_error: str = ''
    last_packet_time: str = ''

    def as_dict(self) -> Dict[str, Any]:
        return {
            'packets': self.packets,
            'errors': self.errors,
            'reconnects': self.reconnects,
            'last_error': self.last_error,
            'last_packet_time': self.last_packet_time,
        }


Sink = Callable[[DataPacket], None]


class BaseCollector(abc.ABC):
    """所有采集器的父类。

    子类需要实现：
        setup()      建立连接（socket / 串口 / LSL inlet / 子进程），失败直接抛异常
        run_loop()   收数据循环，靠 self.should_stop() 判断退出
        teardown()   释放资源（可选）
    可选重写：
        on_stop()    收到 stop() 时先调用，用来「打断」阻塞中的 recv/accept
    """

    name: str = 'base'
    device: str = ''          # data table kind; instance name (sim_ecg) may differ from device (ecg)
    auto_reconnect: bool = True
    reconnect_delay: float = 2.0
    max_reconnect_delay: float = 30.0

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None,
                 name: Optional[str] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._sink: Optional[Sink] = sink
        self.stats = CollectorStats()
        self._state = CollectorState.IDLE
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        if name:
            self.name = name
        self.device = self.config.get('device') or self.device or self.name

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动采集线程（重复调用安全）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_flag.clear()
            self._set_state(CollectorState.STARTING)
            self._thread = threading.Thread(target=self._run, name=f'collect-{self.name}', daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停止采集线程并等待收尾。"""
        with self._lock:
            thread = self._thread
            if thread is None:
                self._set_state(CollectorState.STOPPED)
                return
            self._set_state(CollectorState.STOPPING)
            self._stop_flag.set()
        try:
            self.on_stop()
        except Exception:
            log.exception('[%s] on_stop 出错', self.name)
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning('[%s] 线程 %s 秒内未退出', self.name, timeout)
        self._set_state(CollectorState.STOPPED)

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def state(self) -> CollectorState:
        return self._state

    @property
    def running(self) -> bool:
        return self._state in (CollectorState.RUNNING, CollectorState.RECONNECTING)

    def should_stop(self) -> bool:
        return self._stop_flag.is_set()

    def sleep(self, seconds: float) -> bool:
        """可被 stop() 打断的 sleep；返回 False 表示该退出了。"""
        return not self._stop_flag.wait(seconds)

    def set_sink(self, sink: Optional[Sink]) -> None:
        self._sink = sink

    # ------------------------------------------------------------------ 子类实现
    @abc.abstractmethod
    def setup(self) -> None:
        """建立连接。抛异常会触发重连。"""

    @abc.abstractmethod
    def run_loop(self) -> None:
        """收数据主循环，直到 should_stop() 为真。"""

    def teardown(self) -> None:
        """释放连接资源。"""

    def on_stop(self) -> None:
        """stop() 时调用，用来打断阻塞的 accept/recv。默认交给 teardown()。"""
        self.teardown()

    # ------------------------------------------------------------------ 数据/日志
    def emit(self, payload: Dict[str, Any], t_device: Optional[float] = None,
             raw: Optional[bytes] = None, **meta: Any) -> DataPacket:
        packet = DataPacket(device=self.device, payload=payload, t_device=t_device, raw=raw, meta=dict(meta))
        self.stats.packets += 1
        self.stats.last_packet_time = packet.t_recv
        sink = self._sink
        if sink is not None:
            try:
                sink(packet)
            except Exception:
                log.exception('[%s] sink 处理失败', self.name)
        return packet

    def _set_state(self, state: CollectorState) -> None:
        if state != self._state:
            log.debug('[%s] %s -> %s', self.name, self._state.value, state.value)
            self._state = state

    def _record_error(self, exc: BaseException) -> None:
        self.stats.errors += 1
        self.stats.last_error = f'{type(exc).__name__}: {exc}'

    # ------------------------------------------------------------------ 内部线程
    def _run(self) -> None:
        delay = self.reconnect_delay
        while not self._stop_flag.is_set():
            try:
                self.setup()
                if self._stop_flag.is_set():
                    break
                self._set_state(CollectorState.RUNNING)
                delay = self.reconnect_delay
                self.run_loop()
            except Exception as exc:                      # noqa: BLE001 - 采集线程必须兜住所有异常
                self._record_error(exc)
                if self._stop_flag.is_set():
                    break
                self._set_state(CollectorState.ERROR)
                log.warning('[%s] 采集异常：%s', self.name, exc)
            finally:
                try:
                    self.teardown()
                except Exception:
                    log.exception('[%s] teardown 出错', self.name)
            if self._stop_flag.is_set() or not self.auto_reconnect:
                break
            self.stats.reconnects += 1
            self._set_state(CollectorState.RECONNECTING)
            log.info('[%s] %.1f 秒后重连', self.name, delay)
            if not self.sleep(delay):
                break
            delay = min(delay * 2, self.max_reconnect_delay)
        self._set_state(CollectorState.STOPPED)
        log.info('[%s] 采集线程退出，共收包 %d 个', self.name, self.stats.packets)


class SocketServerCollector(BaseCollector):
    """TCP/UDP 服务端采集器的公共部分。"""

    socket_type: int = 0

    def __init__(self, config=None, sink=None, name=None):
        super().__init__(config, sink, name)
        self.bind = self.config.get('bind', '0.0.0.0')
        self.port = int(self.config.get('port', 0))
        self._sock = None

    def setup(self) -> None:
        import socket

        sock = socket.socket(socket.AF_INET, self.socket_type)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, str(self.port)))
        sock.settimeout(1.0)          # 让 run_loop 有机会检查 should_stop()
        self._sock = sock
        log.info('[%s] 已监听 %s:%s', self.name, self.bind, self.port)

    def teardown(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
