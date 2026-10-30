# -*- coding: utf-8 -*-
"""握力手套采集：串口读原始帧（方案二 —— A0 只搬原始数据）。

来源：老工程 hand1.py 的 SerialDataThread。**只搬协议层**：

* 串口参数：COM5 / 115200 / 8N1（老代码 bytesize 取 [glove_config] bite_size = 8）
* 对时：写 'RTC,YYYY,MM,DD,HH,MM,SS*FF'（GB2312 编码）
* 启动：FF 01 FC     停止：FF 00 FC
* 数据帧：以 \\n 或 \\r 结尾；GB2312 解码；逗号分隔 22 个字段：
  [0]=日期 [1]=时间 [2:20]=18 个原始通道 [20]=电量 [21]=左右手
* 心跳：超过 1 秒没有数据就重发启动指令（老代码行为）
* 前 11 帧作废（老代码 count < 12 的写法保留）

老的 process_frame 里的三段加工（18 通道卡尔曼 / 角度解算 / 压力多项式拟合 + 合成力）
按方案二搬到 A2_analyze_grip.py，本文件不做任何加工。

写库契约：payload 落新表 data_grip_raw（原始 18 通道），
data_grip_l / data_grip_r 由 A2 加工后生成，保持老格式不变。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)

CMD_START = bytes([0xFF, 0x01, 0xFC])
CMD_STOP = bytes([0xFF, 0x00, 0xFC])


class GripCollector(BaseCollector):
    """握力手套串口采集器（只产原始帧）。"""

    name = 'grip'
    device = 'grip'

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None):
        super().__init__(config, sink)
        self.port: str = self.config.get('port', 'COM5')
        self.baudrate: int = int(self.config.get('baudrate', 115200))
        self.bytesize: int = int(self.config.get('bytesize', 8))
        self.encoding: str = self.config.get('encoding', 'gb2312')
        self.skip_frames: int = int(self.config.get('skip_frames', 11))   # 老代码丢掉前 11 帧
        self.heartbeat: float = float(self.config.get('heartbeat_seconds', 1.0))
        self._ser = None
        self._buffer = bytearray()
        self._frame_count = 0

    # ------------------------------------------------------------- 生命周期
    def setup(self) -> None:
        try:
            import serial
        except ImportError as exc:                          # pragma: no cover
            raise RuntimeError('未安装 pyserial：pip install pyserial') from exc

        self._ser = serial.Serial(port=self.port, baudrate=self.baudrate, bytesize=self.bytesize,
                                  parity='N', stopbits=1, timeout=0.5)
        self._buffer = bytearray()
        self._frame_count = 0

        now = datetime.now()
        rtc = f'RTC,{now.year},{now.month:02d},{now.day:02d},{now.hour:02d},{now.minute:02d},{now.second:02d}*FF'
        self._ser.write(rtc.encode(self.encoding))
        time.sleep(0.1)
        self._ser.write(CMD_START)
        log.info('[grip] 串口 %s 已打开，设备时间已对齐 %s', self.port, now.strftime('%Y-%m-%d %H:%M:%S'))

    def run_loop(self) -> None:
        ser = self._ser
        if ser is None:
            raise RuntimeError('串口未打开')
        last_data = time.time()
        last_cmd = last_data
        while not self.should_stop():
            now = time.time()
            if now - last_data > self.heartbeat and now - last_cmd > self.heartbeat:
                ser.write(CMD_START)                        # 老代码的保活指令
                last_cmd = now
                log.debug('[grip] 超过 %.1fs 无数据，重发启动指令', self.heartbeat)
            data = ser.read(ser.in_waiting or 1)
            if not data:
                time.sleep(0.01)
                continue
            last_data = now
            self._buffer.extend(data)
            for raw_frame in self._split_frames():
                self._frame_count += 1
                if self._frame_count <= self.skip_frames:
                    continue
                payload = self._parse_frame(raw_frame)
                if payload is not None:
                    self.emit(payload, raw=raw_frame, side=payload['side'])

    def teardown(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                if ser.is_open:
                    ser.write(CMD_STOP)                     # 老代码的停止指令
                    ser.close()
            except Exception:                               # noqa: BLE001 - 关闭失败不影响收尾
                log.debug('[grip] 关闭串口出错', exc_info=True)

    def on_stop(self) -> None:
        self.teardown()

    # ------------------------------------------------------------- 协议解析
    def _split_frames(self) -> List[bytes]:
        """按 \\n 或 \\r 拆帧（照搬老代码的 buffer 处理）。"""
        frames = []
        while True:
            positions = [p for p in (self._buffer.find(b'\n'), self._buffer.find(b'\r')) if p != -1]
            if not positions:
                break
            end = min(positions) + 1
            frames.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
        return frames

    def _parse_frame(self, raw_frame: bytes) -> Optional[Dict[str, Any]]:
        """22 字段校验 + 组装原始包；解析不出返回 None。"""
        try:
            fields = raw_frame.decode(self.encoding).strip().split(',')
        except UnicodeDecodeError:
            return None
        if len(fields) != 22:
            return None
        try:
            device_time = datetime.strptime(f'{fields[0]} {fields[1]}', '%Y/%m/%d %H:%M:%S')
            raw_values = [int(float(x)) for x in fields[2:20]]      # 18 个原始通道
        except (ValueError, IndexError):
            return None
        return {
            'time': device_time.strftime('%Y-%m-%d %H:%M:%S'),
            'raw': raw_values,
            'battery': fields[20],
            'side': (fields[21] or 'L').strip().upper()[:1] or 'L',
            'send_time': now_str(),
        }
