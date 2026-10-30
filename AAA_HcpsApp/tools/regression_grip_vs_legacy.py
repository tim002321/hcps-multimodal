# -*- coding: utf-8 -*-
"""差分测试：新的 A2_analyze_grip 与老的 hand1.process_frame 逐帧对比。

用同一批合成原始帧（含一次角度回绕）分别喂给两套实现，比较：
angle / press / Synthesis_F 是否一致。
"""
import math
import random
import sys

OLD_ROOT = r'F:\hcpsproject\PyCharmMiscProject'
NEW_ROOT = r'F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp'
sys.path.insert(0, OLD_ROOT)
sys.path.insert(0, NEW_ROOT)

import hand1                                    # 老算法（只 import，不开窗口）
from core.A2_analyze import GripProcessor  # 新算法

PROCESS_NOISE, MEASUREMENT_NOISE = 0.01, 2.0


class FakeOld:
    """给老 process_frame 用的最小宿主对象（不初始化 Qt）。"""

    def __init__(self):
        self.hand_states = {
            side: {'angle_initialized': False, 'angle': [0, 0, 0],
                   'kalman_filters': [hand1.KalmanFilter(PROCESS_NOISE, MEASUREMENT_NOISE) for _ in range(18)]}
            for side in ('L', 'R')
        }


def make_raw_frames(count=200):
    """造 count 帧原始数据：左右手轮流，第 60 帧起角度从 -1799 跳到 +1799（测回绕）。"""
    random.seed(7)
    frames = []
    for i in range(count):
        side = 'L' if i % 2 == 0 else 'R'
        if i < 60:
            angle0 = -1799
        else:
            angle0 = 1799
        angles = [angle0, int(600 * math.sin(i / 9.0)), int(400 * math.cos(i / 13.0))]
        pressures = [int(1200 + 800 * math.sin(i / 11.0 + k) + random.gauss(0, 5)) for k in range(15)]
        frames.append((side, angles + pressures))
    return frames


def main():
    old = FakeOld()
    new = GripProcessor({'kalman': {'process_noise': PROCESS_NOISE,
                                    'measurement_noise': MEASUREMENT_NOISE}})
    frames = make_raw_frames()

    bad = 0
    max_diff = {'angle': 0.0, 'press': 0.0, 'force': 0.0}
    for i, (side, raw) in enumerate(frames):
        fields = ['2026/09/15', '14:34:39'] + [str(v) for v in raw] + ['87', side]
        old_result = hand1.SerialDataThread.process_frame(old, fields)
        _, old_press, _, old_angle, old_force, _ = old_result

        new_result = new.process(raw, side)
        new_angle, new_press, new_force = new_result['angle'], new_result['press'], new_result['synthesis_force']

        diff_angle = max(abs(a - b) for a, b in zip(old_angle, new_angle))
        diff_press = max(abs(a - b) for a, b in zip(old_press, new_press))
        diff_force = abs(old_force - new_force)
        max_diff['angle'] = max(max_diff['angle'], diff_angle)
        max_diff['press'] = max(max_diff['press'], diff_press)
        max_diff['force'] = max(max_diff['force'], diff_force)
        if diff_angle > 1e-9 or diff_press > 1e-6 or diff_force > 1e-6:
            bad += 1
            if bad <= 3:
                print(f'  第 {i} 帧不一致：angle {old_angle} vs {new_angle}；'
                      f'press差={diff_press:.3e}；force {old_force} vs {new_force}')

    print(f'对比帧数：{len(frames)}，不一致帧数：{bad}')
    print(f'最大偏差：angle={max_diff["angle"]:.3e}  press={max_diff["press"]:.3e}  '
          f'Synthesis_F={max_diff["force"]:.3e}')
    print('[通过] 新 A2 与老算法逐帧一致' if bad == 0 else '[失败] 存在不一致帧')
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
