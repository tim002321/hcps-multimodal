# -*- coding: utf-8 -*-
"""眼动仪接收（aSeeStudio 字符串报文 → UDP 收流 → 解析 → 入库）。

数据来源：aSeeStudio 通过 UDP 端口转发，**一包一帧、纯字符串、没有换行符**，
字段顺序固定为 15 项（前 14 项定长，最后一项 AOI 名称可变长）：

    {"时间戳","推荐眼注视点是否有效","推荐眼注视点X值","推荐眼注视点Y值",
     "左眼注视点是否有效","左眼注视点X值","左眼注视点Y值",
     "右眼注视点是否有效","右眼注视点X值","右眼注视点Y值",
     "左眼瞳孔大小毫米值","右眼瞳孔大小毫米值",
     "左眼眨眼状态","右眼眨眼状态",[AOI名称]}

字段解析在 `devices/eye_protocol.py`（纯函数、可离线测），本文件只负责：
监听端口 → 收包 → 调解析 → 调 `recorder.record_eye()` → 维护统计。

为什么用阻塞 socket + 线程，而不是 asyncio：UDP 收流是纯 IO、没有并发需求，
`settimeout(0.3)` 的阻塞循环停止响应快、不会像 `sock_recvfrom` 那样在退出时
卡在事件循环上；也不需要和 BLE 那个 loop 抢生命周期。

单独联调（不启界面）：`tools/eye/listen_eye.py`。
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any, Dict, Optional

from devices.eye_protocol import EyeFrame, parse_asee, to_text


class EyeReceiver(threading.Thread):
    """眼动 UDP 接收线程。`start()` 即开始收流，`stop()` 会优雅退出。"""

    def __init__(self, recorder=None, port: int = 8003, bind_host: str = '0.0.0.0',
                 status=None, debug: bool = False, idle_warn_sec: float = 10.0,
                 max_buffer: int = 65535, report_interval: float = 1.0):
        super().__init__(name='eye-receiver', daemon=True)
        self.recorder = recorder
        self.port = int(port)
        self.bind_host = bind_host
        self.status = status
        self.debug = debug
        self.idle_warn_sec = float(idle_warn_sec)
        self.max_buffer = int(max_buffer)
        self.report_interval = float(report_interval)

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._bound = False        # 当前是否正在监听（stop 后为 False）
        self._listen_ok = False    # 是否成功绑定过 —— stop 之后看统计用这个
        self._started_at = time.time()

        self.received = 0          # 收到的包数
        self.parsed = 0            # 解析成功的帧数
        self.failed = 0            # 解析失败的包数
        self.written = 0           # 实际写入数据库的行数
        self.write_errors = 0      # 写库异常次数（数据库被锁之类）
        self.last_at = 0.0         # 最后一个包到达的本机时间
        self.last_error = ''
        self.peer = ''             # 发送方地址，现场确认是不是这台机器在发
        self._last_frame: Optional[EyeFrame] = None
        self._last_report = 0.0

    # ------------------------------------------------------------------
    # 统计与状态
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            frame = self._last_frame
            return {
                'bound': self._bound,
                'listen_ok': self._listen_ok,
                'port': self.port,
                'peer': self.peer,
                'received': self.received,
                'parsed': self.parsed,
                'failed': self.failed,
                'written': self.written,
                'write_errors': self.write_errors,
                'last_at': self.last_at,
                'last_error': self.last_error,
                'last_summary': frame.summary() if frame is not None else '',
                'last_aois': list(frame.aois) if frame is not None else [],
                'last_pupil': frame.pupil if frame is not None else None,
                'last_timestamp': frame.timestamp if frame is not None else None,
                'elapsed': max(0.0, time.time() - self._started_at),
                'rate': self._rate(),
            }

    def _rate(self) -> float:
        """平均帧率（包/秒）。没收到包时为 0，不拿它做除零。"""
        elapsed = max(0.0, time.time() - self._started_at)
        if elapsed < 1.0:
            return 0.0
        return round(self.received / elapsed, 1)

    def latest(self) -> Optional[EyeFrame]:
        with self._lock:
            return self._last_frame

    def _set_status(self, state: str, detail: str = '') -> None:
        if self.status is not None:
            self.status.set('eye', state, self.peer, detail)

    def _report(self, force: bool = False) -> None:
        """按间隔刷新状态栏文字，别每帧都动（60Hz 会把界面刷爆）。"""
        now = time.time()
        if not force and now - self._last_report < self.report_interval:
            return
        self._last_report = now
        detail = '收包 {}/解析 {}/失败 {}/写入 {}'.format(
            self.received, self.parsed, self.failed, self.written)
        self._set_status('connected', detail)

    # ------------------------------------------------------------------
    # 收流
    # ------------------------------------------------------------------
    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind_host, self.port))
        except OSError as exc:
            self._bound = False
            self._set_status('error', 'UDP {} 绑定失败：{}'.format(self.port, exc))
            print('[眼动] UDP {} 绑定失败：{}'.format(self.port, exc), flush=True)
            sock.close()
            return

        sock.settimeout(0.3)
        self._sock = sock
        self._bound = True
        self._listen_ok = True
        self._started_at = time.time()
        self._set_status('listening', 'UDP {} 已监听，等待 aSeeStudio 转发'.format(self.port))
        print('[眼动] UDP {} 已监听，等待数据'.format(self.port), flush=True)

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(self.max_buffer)
                except socket.timeout:
                    self._check_idle()
                    continue
                except OSError as exc:
                    if not self._stop_event.is_set():
                        self._set_status('error', '接收异常：{}'.format(exc))
                        print('[眼动] 接收异常：{}'.format(exc), flush=True)
                    break
                self._handle(data, addr)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
            self._bound = False
            if not self._stop_event.is_set():
                self._set_status('lost', '接收意外中断')
            else:
                self._set_status('idle', '接收已停止')

    def _check_idle(self) -> None:
        """端口开着但一直没包：多半是 aSeeStudio 还没点「开始转发」。"""
        if self.received > 0 or self.status is None:
            return
        waited = time.time() - self._started_at
        if waited >= self.idle_warn_sec:
            self._set_status(
                'listening',
                '已监听 {:.0f} 秒但仍无数据：确认 aSeeStudio 的目标地址与端口'.format(waited))

    def _handle(self, data: bytes, addr) -> None:
        now = time.time()
        with self._lock:
            self.received += 1
            self.last_at = now
            if addr:
                try:
                    self.peer = '{}:{}'.format(addr[0], addr[1])
                except (IndexError, TypeError):
                    self.peer = str(addr)

        frame = parse_asee(data)
        if frame is None:
            text = to_text(data)
            with self._lock:
                self.failed += 1
                self.last_error = '无法解析（{} 字符）：{}'.format(len(text), text[:60])
            if self.debug:
                print('[眼动] 丢包：{}'.format(text[:120]), flush=True)
            self._report()
            return

        with self._lock:
            self.parsed += 1
            self._last_frame = frame

        if self.recorder is not None:
            try:
                if self.recorder.record_eye(frame):
                    with self._lock:
                        self.written += 1
            except Exception as exc:            # noqa: BLE001 - 写库失败不能中断收流
                with self._lock:
                    self.write_errors += 1
                    self.last_error = '写库失败：{}'.format(exc)
                if self.debug:
                    print('[眼动] 写库失败：{}'.format(exc), flush=True)

        if self.debug:
            print('[眼动] {} {}'.format(frame.timestamp_raw or '-', frame.summary()), flush=True)
        self._report()

    def stop(self) -> None:
        """请求退出。阻塞等待最多 2 秒，等不到就让线程随进程结束。"""
        self._stop_event.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()        # 让阻塞中的 recvfrom 立刻抛错退出
            except OSError:
                pass


def start_eye(recorder=None, port: int = 8003, bind_host: str = '0.0.0.0',
              status=None, debug: bool = False) -> EyeReceiver:
    """创建并启动接收线程，返回实例（调用方负责 stop）。"""
    receiver = EyeReceiver(recorder=recorder, port=port, bind_host=bind_host,
                           status=status, debug=debug)
    receiver.start()
    return receiver


# 兼容旧调用名：骨架版本里这里是 async def start_eye(...)，签名不同。
async def start_eye_async(*args, **kwargs) -> None:      # pragma: no cover - 兼容壳
    """旧 asyncio 调用点的兼容壳：内部起线程，然后一直等到被取消。"""
    import asyncio

    receiver = start_eye(*args, **kwargs)
    try:
        while receiver.is_alive():
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        receiver.stop()
        raise
