# -*- coding: utf-8 -*-
"""表情识别的子进程封装：界面进程不加载 TensorFlow。

`tools/face_cli.py --serve` 是个常驻小进程，从 stdin 收图片路径、往 stdout 吐一行 JSON。
子进程第一次启动要把 TensorFlow 和权重热起来（约 5~10 秒），之后每张图百毫秒级。

这么做是因为 tf + PyQt5 同进程在 Windows 上有概率直接访问违例（弹"内存不能为 read"），
把 tf 隔离出去，界面进程就不会被带崩。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Optional

from PyQt5.QtCore import QObject, QProcess, pyqtSignal

log = logging.getLogger(__name__)


class FaceWorker(QObject):
    """驱动表情识别子进程。"""

    result = pyqtSignal(dict)      # 一张图的识别结果
    failed = pyqtSignal(str)       # 子进程启动失败 / 识别报错
    ready = pyqtSignal()           # 子进程模型加载完成

    def __init__(self, cli_path: str, config_path: str, python_exe: Optional[str] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.cli_path = cli_path
        self.config_path = config_path
        self.python_exe = python_exe or sys.executable
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.errorOccurred.connect(self._on_error)
        self._buffer = bytearray()
        self._started = False

    # ------------------------------------------------------------- 控制
    @property
    def running(self) -> bool:
        return self.process.state() != QProcess.NotRunning

    def start(self) -> None:
        if self.running:
            return
        if not os.path.exists(self.cli_path):
            self.failed.emit(f'找不到识别脚本：{self.cli_path}')
            return
        log.info('[face] 启动识别子进程（TensorFlow 在里面，界面进程不加载）')
        self.process.start(self.python_exe, [self.cli_path, '--serve', '--config', self.config_path])
        self._started = True

    def request(self, image_path: str) -> None:
        """丢一张图给子进程；结果通过 result 信号回来。"""
        self.start()
        if not self.running:
            return
        self.process.write((str(image_path) + '\n').encode('utf-8'))

    def request_frame(self, jpeg_bytes: bytes, frame_id: int) -> None:
        """丢一整帧（摄像头 JPEG 字节）给子进程——实时监测走这条，不用落盘再读。"""
        self.start()
        if not self.running:
            return
        payload = json.dumps({'id': int(frame_id),
                              'b64': base64.b64encode(jpeg_bytes).decode('ascii')})
        self.process.write((payload + '\n').encode('utf-8'))

    def stop(self, timeout: int = 3000) -> None:
        if not self.running:
            return
        self.process.write(b'__quit__\n')
        if not self.process.waitForFinished(timeout):
            self.process.kill()

    # ------------------------------------------------------------- 子进程回调
    def _on_stdout(self) -> None:
        self._buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b'\n' in self._buffer:
            line, _, rest = self._buffer.partition(b'\n')
            self._buffer = bytearray(rest)
            text = line.decode('utf-8', 'ignore').strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                log.debug('[face] 子进程输出无法解析：%s', text[:120])
                continue
            if payload.get('ready'):
                self.ready.emit()
            elif 'error' in payload and 'emotions' not in payload:
                self.failed.emit(str(payload['error']))
            else:
                self.result.emit(payload)

    def _on_stderr(self) -> None:
        raw = bytes(self.process.readAllStandardError()).decode('utf-8', 'ignore').strip()
        if raw:
            log.debug('[face cli] %s', raw.splitlines()[-1][:200])

    def _on_error(self, error) -> None:
        if error == QProcess.FailedToStart:
            self.failed.emit(f'识别子进程启动失败：{self.python_exe}')
        elif error == QProcess.Crashed:
            self.failed.emit('识别子进程崩溃（TensorFlow 侧），下次识别会自动重启')
