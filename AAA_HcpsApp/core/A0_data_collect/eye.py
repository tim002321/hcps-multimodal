# -*- coding: utf-8 -*-
"""眼动采集：UDP 服务端 + SmartEye 二进制解包。

来源：老工程 data_udp.py 的 Data_Udp_Thread + ReceiveHandler1。
**解码表和解包函数是从老代码原样搬过来的**（DataType_dict / decode_eye /
Data_decode / Smarteye_decode），一个字都没改——这是整个系统里最贵的资产，别动它。

只改了外壳：
  * 用 BaseCollector 的生命周期替代 udp_eye_running 全局 Event
  * 不再写数据库、不再往队列里塞 predict_data，统一 emit(DataPacket)
  * 去掉了老代码 `Smarteye_decode(eval(str(recvfrom())))` 里绕一圈的 str/eval，
    直接吃 BytesIO（等价，因为 eval(str(b'..')) 就是还原原字节）

写库契约（A1 层负责落库）：
    data_udp.value = json.dumps(解码字典 + send_time).encode('utf-8')   # BLOB
"""
from __future__ import annotations

import io
import json
import logging
import socket
import struct
from typing import Any, Dict, Optional

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 以下解码表与函数：原样搬自老工程 data_udp.py（勿改）
# ---------------------------------------------------------------------------
DataType_dict = {
    'SEType_Point3D': [['>d', '>d', '>d'], [8, 8, 8]],
    'SEType_float': [['>d'], [8]],
    'SEType_Vect3D': [['>d', '>d', '>d'], [8, 8, 8]],
    'SEType_u8': [['>B'], [1]],
    'SEType_u16': [['>H'], [2]],
    'SEType_u32': [['>I'], [4]],
    'SEType_u64': [['>Q'], [8]],
    'SEType_WorldIntersection': [['>d', '>d', '>d', '>d', '>d', '>d', '>H', '>B'], [8, 8, 8, 8, 8, 8, 2, 1]],
    'SEType WorldIntersections': [['>H', '>d', '>d', '>d', '>d', '>d', '>d', '>H', '>B'],
                                  [2, 8, 8, 8, 8, 8, 8, 2, 1]]
}


def decode_eye(data):  # 数据流解包
    # 创建 BytesIO 对象
    stream = io.BytesIO(data)
    # 解析头部数据
    id = struct.unpack('>I', stream.read(4))[0]
    type = struct.unpack('>H', stream.read(2))[0]
    len = struct.unpack('>H', stream.read(2))[0]
    map = {}
    try:
        while True:
            # 循环解析子包
            subId = stream.read(2).hex()      # 此处的打包方式为高位在后
            subLen = struct.unpack('>H', stream.read(2))[0]
            if subLen == 0:
                break
            bytes_ = stream.read(subLen)
            map[subId] = bytes_
    except struct.error:
        # 解析完成
        return map
    return map


def Data_decode(data, EnumValue, DataType):  # 指定参数解码
    """
        根据Table A.1获取数据的EnumValue
        使用EnumValue从data中获取须解码的数据
        根据Table 3.2确定数据的组成即划分方法，从而确定split，之后完成解码
    """
    local = 0
    result = []
    split = DataType[1]
    now_data = data[EnumValue]
    for i in range(len(split)):
        result.append(struct.unpack(DataType[0][i], now_data[:split[i]])[0])
        now_data = now_data[split[i]:]
        local = local + split[i]
    return result


def Smarteye_decode(data):
    decode_result = decode_eye(data)
    # 指定参数列表
    eye_ids = ['0001', '0002', '0003', '0004', '0005', '003d', '003e', '003f', '00e0',
               '00e1', '00e2', '00e3', '00e4', '00e5', '00e6', '00e7', '00e8', '00e9',
               '00ea', '00eb', '0050', '0051', '0052', '0053', '0054', '0055', '0060',
               '0061', '0062', '0063', '0064', '0065', '0066', '0067', '0068', '0069',
               '006a', '006b', '0087', '0088', '0089', '008c', '008d', '008e', '0093',
               '0094', '0098', '0099', '009d', '009e']
    eye_dict = {'0001': 'SEType_u32', '0002': 'SEType_u32', '0003': 'SEType_u64', '0004': 'SEType_u64',
                '0005': 'SEType_float', '003d': 'SEType_u32', '003e': 'SEType_u32', '003f': 'SEType_u32',
                '0050': 'SEType_float', '0051': 'SEType_float', '0052': 'SEType_float', '0053': 'SEType_float',
                '0054': 'SEType_float', '0055': 'SEType_float', '0060': 'SEType_float', '0061': 'SEType_float',
                '0062': 'SEType_float', '0063': 'SEType_float', '0064': 'SEType_float', '0065': 'SEType_float',
                '0066': 'SEType_float', '0067': 'SEType_float', '0068': 'SEType_float', '0069': 'SEType_float',
                '006a': 'SEType_float', '006b': 'SEType_float', '0087': 'SEType_float', '0088': 'SEType_float',
                '0089': 'SEType_float', '008c': 'SEType_float', '008d': 'SEType_float', '008e': 'SEType_float',
                '0093': 'SEType_float', '0094': 'SEType_float', '0098': 'SEType_float', '0099': 'SEType_float',
                '009d': 'SEType_float', '009e': 'SEType_float', '00e0': 'SEType_u64', '00e1': 'SEType_u64',
                '00e2': 'SEType_float', '00e3': 'SEType_float', '00e4': 'SEType_float', '00e5': 'SEType_float',
                '00e6': 'SEType_u64', '00e7': 'SEType_u64', '00e8': 'SEType_float', '00e9': 'SEType_float',
                '00ea': 'SEType_float', '00eb': 'SEType_float'}
    eye_namedict = {'0001': 'FrameNumber', '0002': 'EstimatedDelay', '0003': 'TimeStamp', '0004': 'UserTimeStamp',
                    '0005': 'FrameRate', '003d': 'Saccade', '003e': 'Fixation', '003f': 'Blink',
                    '00e0': 'EyelidOpening', '00e1': 'EyelidOpeningQ', '00e2': 'LeftEyelidOpening',
                    '00e3': 'LeftEyelidOpeningQ', '00e4': 'RightEyelidOpening', '00e5': 'RightEyelidOpeningQ',
                    '00e6': 'PupilDiameter', '00e7': 'PupilDiameterQ', '00e8': 'LeftPupilDiameter',
                    '00e9': 'LeftPupilDiameterQ', '00ea': 'RightPupilDiameter', '00eb': 'RightPupilDiameterQ',
                    '0050': 'FilteredPupilDiameter', '0051': 'FilteredPupilDiameterQ',
                    '0052': 'FilteredLeftPupilDiameter', '0053': 'FilteredLeftPupilDiameterQ',
                    '0054': 'FilteredRightPupilDiameter', '0055': 'FilteredRightPupilDiameterQ',
                    '0060': 'EstimatedLeftGazeDirectionQ', '0061': 'EstimatedLeftGazeHeading',
                    '0062': 'EstimatedLeftGazePitch', '0063': 'EstimatedRightGazeDirectionQ',
                    '0064': 'EstimatedRightGazeHeading', '0065': 'EstimatedRightGazePitch',
                    '0066': 'FilteredEstimatedGazeHeading', '0067': 'FilteredEstimatedGazePitch',
                    '0068': 'FilteredEstimatedLeftGazeHeading', '0069': 'FilteredEstimatedLeftGazePitch',
                    '006a': 'FilteredEstimatedRightGazeHeading', '006b': 'FilteredEstimatedRightGazePitch',
                    '0087': 'LeftBlinkClosingMidTime', '0088': 'LeftBlinkOpeningMidTime',
                    '0089': 'LeftBlinkClosingAmplitude', '008c': 'LeftBlinkOpeningAmplitude',
                    '008d': 'LeftBlinkClosingSpeed', '008e': 'LeftBlinkOpeningSpeed',
                    '0093': 'RightBlinkClosingMidTime', '0094': 'RightBlinkOpeningMidTime',
                    '0098': 'RightBlinkClosingAmplitude', '0099': 'RightBlinkOpeningAmplitude',
                    '009d': 'RightBlinkClosingSpeed', '009e': 'RightBlinkOpeningSpeed'}
    eye_paremeters = {}
    for id in eye_ids:
        try:
            numvalue = id
            datatype = eye_dict[id]
            data = Data_decode(decode_result, EnumValue=numvalue, DataType=DataType_dict[datatype])
            eye_paremeters[eye_namedict[id]] = data[0]
        except Exception as e:
            log.debug('眼动解码缺字段 %s：%s', eye_namedict[id], e)
            eye_paremeters[eye_namedict[id]] = 0
    eye_data = eye_paremeters  # 字典
    return eye_data


# ---------------------------------------------------------------------------
# 采集器外壳
# ---------------------------------------------------------------------------
class EyeCollector(BaseCollector):
    """眼动 UDP 服务端采集器。"""

    name = 'eye'
    buf_size = 100000

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None):
        super().__init__(config, sink)
        self.bind: str = self.config.get('bind', '0.0.0.0')
        self.port: int = int(self.config.get('port', 8003))
        self.frame_rate: int = int(self.config.get('frame_rate', 30))
        self._sock: Optional[socket.socket] = None

    def setup(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self.port))
        sock.settimeout(1.0)
        self._sock = sock
        log.info('[eye] UDP 服务端已监听 %s:%s', self.bind, self.port)

    def run_loop(self) -> None:
        sock = self._sock
        if sock is None:
            raise RuntimeError('socket 未初始化')
        while not self.should_stop():
            try:
                data, addr = sock.recvfrom(self.buf_size)
            except socket.timeout:
                continue
            if not data:
                continue
            try:
                payload = Smarteye_decode(data)
            except Exception as exc:
                log.warning('[eye] 解包失败（%s）：%s', addr, exc)
                continue
            payload['send_time'] = now_str()
            self.emit(payload, t_device=payload.get('TimeStamp'), raw=data,
                      frame_number=payload.get('FrameNumber'))

    def teardown(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
