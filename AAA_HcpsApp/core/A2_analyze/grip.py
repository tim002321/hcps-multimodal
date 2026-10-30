# -*- coding: utf-8 -*-
"""握力加工（A2）：data_grip_raw 原始帧 -> 老格式 data_grip_l / data_grip_r。

三段加工全部照搬老工程 hand1.py 的 SerialDataThread.process_frame 与 KalmanFilter：

1. 18 个通道各过一遍一维卡尔曼滤波（左右手各一套滤波器，参数可配）
2. 前 3 个通道是关节角度：先做 ±3600 回绕处理，再 ÷10 转成度，绝对值 ≤0.2 记 0
3. 后 15 个通道是压力：÷100 后代入四次多项式（左右手系数不同，末项 -28 / -51），
   结果 <1 记 0
4. Synthesis_F 合成力 = p10*3 + p11*4 + p13*3 + p12 + p14*4（老代码里的经验权重）

输出写回 data_grip_l / data_grip_r，字段与老库完全一致：
    time  = 设备时间
    angle = str([三个角度])
    press = str([15 个压力值])
"""
from __future__ import annotations

import ast
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    'kalman': {'process_noise': 0.01, 'measurement_noise': 2.0},
    'angle_scale': 10.0,          # 老代码里除以 10
    'angle_dead_zone': 0.2,       # 老代码里 ≤0.2 记 0
    'pressure_scale': 100.0,      # 老代码里除以 100
    'pressure_offset': {'L': 28.0, 'R': 51.0},
}


class Kalman1D:
    """一维卡尔曼滤波，公式与老工程 hand1.KalmanFilter 完全一致。"""

    def __init__(self, process_noise: float = 0.01, measurement_noise: float = 1.0):
        self.x = 0.0
        self.P = 1.0
        self.Q = process_noise
        self.R = measurement_noise
        self.F = 1.0
        self.H = 1.0

    def update(self, measurement: float) -> float:
        x_pred = self.F * self.x
        p_pred = self.F * self.P * self.F + self.Q
        y = measurement - self.H * x_pred
        s = self.H * p_pred * self.H + self.R
        k = p_pred * self.H / s
        self.x = x_pred + k * y
        self.P = (1 - k * self.H) * p_pred
        return self.x


class GripProcessor:
    """左右手各一套状态：18 个卡尔曼滤波器 + 角度基准。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(DEFAULT_CONFIG)
        for key, value in (config or {}).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **value}
            else:
                cfg[key] = value
        self.cfg = cfg
        self._states = {
            side: {'initialized': False, 'angle': [0, 0, 0], 'filters': self._new_filters()}
            for side in ('L', 'R')
        }

    def _new_filters(self) -> List[Kalman1D]:
        kalman = self.cfg['kalman']
        return [Kalman1D(kalman['process_noise'], kalman['measurement_noise']) for _ in range(18)]

    def process(self, raw_values: List[int], side: str) -> Dict[str, Any]:
        """原始 18 通道 -> {'side', 'angle', 'press', 'synthesis_force'}"""
        side = 'R' if str(side).upper().startswith('R') else 'L'
        state = self._states[side]
        filtered = [int(round(f.update(v))) for f, v in zip(state['filters'], raw_values)]
        changed_angle = self._angles(state, filtered[:3])
        press = self._pressures(filtered[3:18], side)
        synthesis = press[10] * 3 + press[11] * 4 + press[13] * 3 + press[12] + press[14] * 4
        return {'side': side, 'angle': changed_angle, 'press': press, 'synthesis_force': synthesis}

    def _angles(self, state: Dict[str, Any], filtered_angles: List[int]) -> List[float]:
        """老代码的角度解算：首帧只建基准，之后算增量并处理 ±3600 回绕。"""
        scale = float(self.cfg['angle_scale'])
        dead = float(self.cfg['angle_dead_zone'])
        if not state['initialized']:
            state['angle'] = list(filtered_angles)
            state['initialized'] = True
            return [0, 0, 0]
        changed = [0, 0, 0]
        for i in range(3):
            current, new = state['angle'][i], filtered_angles[i]
            if current < -1000 and new > 1000:
                delta = (-3600 - current + new) / scale
            elif current > 1000 and new < -1000:
                delta = (3600 - current + new) / scale
            else:
                delta = (new - current) / scale
            changed[i] = 0 if abs(delta) <= dead else delta
        state['angle'] = list(filtered_angles)
        return changed

    def _pressures(self, filtered_press: List[int], side: str) -> List[float]:
        """老代码的压力换算：÷100 代入四次多项式，<1 记 0，补齐 15 路。"""
        scale = float(self.cfg['pressure_scale'])
        offset = float(self.cfg['pressure_offset'].get(side, 0.0))
        out: List[float] = []
        for value in filtered_press:
            v = value / scale
            y = (1.12558144E-06 * v ** 4 - 1.10078544E-03 * v ** 3 + 4.04193612E-01 * v ** 2
                 - 6.92249975E+01 * v + 5.05951921E+03 - offset)
            if y < 1:
                y = 0
            out.append(max(0, y))
        while len(out) < 15:
            out.append(0)
        return out[:15]


def process_raw_table(db_path: str, config: Optional[Dict[str, Any]] = None,
                      write_legacy: bool = True, limit: Optional[int] = None) -> Dict[str, int]:
    """把 data_grip_raw 加工一遍并写入 data_grip_l / data_grip_r。

    返回 {'raw': 读取条数, 'L': 写左手条数, 'R': 写右手条数}
    """
    from core.A1_data_store import insert_legacy_grip_rows

    processor = GripProcessor(config)
    conn = sqlite3.connect(db_path)
    try:
        sql = 'SELECT "side", "device_time", "raw", "created_date" FROM data_grip_raw ORDER BY "id"'
        if limit:
            sql += f' LIMIT {int(limit)}'
        rows = conn.execute(sql).fetchall()
        buffered: Dict[str, List[tuple]] = {'L': [], 'R': []}
        for side, device_time, raw_json, created in rows:
            try:
                raw_values = json.loads(raw_json)
            except (TypeError, ValueError):
                try:
                    raw_values = ast.literal_eval(raw_json)          # 兼容 repr 文本
                except (ValueError, SyntaxError):
                    continue
            if not isinstance(raw_values, list) or len(raw_values) < 18:
                continue
            try:
                result = processor.process(raw_values[:18], side or 'L')
            except Exception:
                log.exception('握力加工失败，跳过该帧')
                continue
            buffered[result['side']].append((
                device_time or created,
                str(result['angle']),
                str(result['press']),
                created or device_time,
            ))
        written = {'L': 0, 'R': 0}
        if write_legacy:
            for side, items in buffered.items():
                if items:
                    insert_legacy_grip_rows(conn, side, items)
                    written[side] = len(items)
            conn.commit()
        return {'raw': len(rows), 'L': written['L'], 'R': written['R']}
    finally:
        conn.close()
