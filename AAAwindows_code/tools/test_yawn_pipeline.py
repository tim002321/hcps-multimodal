#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打哈欠时序状态机的单测（不需要模型、不需要摄像头、不需要 torch）。

    python tools/test_yawn_pipeline.py

覆盖计划里定的四条规则，外加疲劳分级：
  1. 持续张嘴 >= yawn_min_sec → 计一次
  2. 只是闪一下                → 不计
  3. 冷却期内再张嘴            → 不重复计
  4. 长时间无人脸              → 回到「无脸」态，不误计
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vision.yawn_logic import (FATIGUE_MILD, FATIGUE_NORMAL, FATIGUE_SEVERE,  # noqa: E402
                               STATE_NORMAL, STATE_NO_FACE, STATE_OPEN,
                               STATE_YAWNING, YawnLogic)

FPS = 10.0            # 状态机按 10Hz 喂（与线上 6~7fps 同量级）
DT = 1.0 / FPS

_results = []


def check(name: str, condition: bool, detail: str = '') -> None:
    _results.append((name, bool(condition), detail))
    mark = 'OK' if condition else 'FAIL'
    print('  [{}] {}{}'.format(mark, name, ('  ' + detail) if detail and not condition else ''))


def feed(logic: YawnLogic, start: float, seconds: float, prob: float,
         has_face: bool = True) -> tuple:
    """按固定帧率喂一段，返回 (结束时刻, 最后一个状态)。"""
    t = start
    status = logic.update(t, prob, has_face)
    steps = max(1, int(round(seconds * FPS)))
    for _ in range(steps):
        t += DT
        status = logic.update(t, prob, has_face)
    return t, status


def case_continuous_yawn() -> None:
    """规则 1：连续张嘴 2 秒 → 计一次，且状态为「打哈欠」。"""
    print('\n=== 用例 1：连续张嘴 2 秒 ===')
    logic = YawnLogic()
    t = 0.0
    t, status = feed(logic, t, 0.4, 0.05)                      # 先闭口
    check('闭口时状态为正常', status.state == STATE_NORMAL, status.state)

    t, status = feed(logic, t, 0.3, 0.9)                       # 张嘴 0.3s，还不到 1.5s
    check('刚张嘴时只算「张嘴中」', status.state == STATE_OPEN, status.state)
    check('此时尚未计数', status.yawn_count == 0, str(status.yawn_count))

    t, status = feed(logic, t, 2.0, 0.9)                       # 继续张嘴到 2.3s
    check('持续张嘴后计一次哈欠', status.yawn_count == 1, str(status.yawn_count))
    check('状态为打哈欠', status.state == STATE_YAWNING, status.state)


def case_short_flash() -> None:
    """规则 2：只张开 0.5 秒 → 不计（说话、张嘴呼吸的典型形态）。"""
    print('\n=== 用例 2：张嘴闪一下 0.5 秒 ===')
    logic = YawnLogic()
    t = 0.0
    for _ in range(5):                                          # 重复 5 次短促张嘴
        t, status = feed(logic, t, 0.5, 0.9)
        t, status = feed(logic, t, 0.5, 0.05)
    check('5 次短促张嘴都不计数', status.yawn_count == 0, str(status.yawn_count))
    check('状态回到正常', status.state == STATE_NORMAL, status.state)


def case_cooldown() -> None:
    """规则 3：计数后的冷却期内再张嘴，不重复计。"""
    print('\n=== 用例 3：冷却期内重复张嘴 ===')
    logic = YawnLogic(cooldown_sec=5.0)
    t = 0.0
    t, status = feed(logic, t, 2.0, 0.9)
    check('第一次哈欠已计数', status.yawn_count == 1, str(status.yawn_count))

    t, status = feed(logic, t, 0.3, 0.05)                       # 闭嘴
    t, status = feed(logic, t, 2.0, 0.9)                        # 冷却期内再张嘴 2s
    check('冷却期内不重复计数', status.yawn_count == 1, str(status.yawn_count))

    t, status = feed(logic, t, 6.0, 0.05)                       # 等过冷却
    t, status = feed(logic, t, 2.0, 0.9)                        # 再打一次
    check('冷却结束后可再次计数', status.yawn_count == 2, str(status.yawn_count))


def case_no_face() -> None:
    """规则 4：长时间无人脸 → 回「无脸」态，且不会凭空计数。"""
    print('\n=== 用例 4：长时间无人脸 ===')
    logic = YawnLogic()
    t = 0.0
    t, status = feed(logic, t, 3.0, 0.0, has_face=False)
    check('无人脸时状态为「未检测到人脸」', status.state == STATE_NO_FACE, status.state)
    check('无人脸不产生哈欠', status.yawn_count == 0, str(status.yawn_count))

    # 张嘴过程中突然丢脸（被试转头）：不应把丢失期间算成持续张嘴
    t, status = feed(logic, t, 0.5, 0.9)
    t, status = feed(logic, t, 3.0, 0.0, has_face=False)
    check('张嘴中丢脸不误计', status.yawn_count == 0, str(status.yawn_count))
    check('丢脸后状态为无脸', status.state == STATE_NO_FACE, status.state)


def case_fatigue_level() -> None:
    """疲劳分级：窗口内 0~1 正常、2~3 轻度、>=4 重度。"""
    print('\n=== 用例 5：疲劳分级 ===')
    logic = YawnLogic(cooldown_sec=1.0, mild_count=2, severe_count=4)
    t = 0.0
    t, status = feed(logic, t, 2.0, 0.9)
    check('1 次时仍为正常', status.fatigue == FATIGUE_NORMAL, status.fatigue)

    t, status = feed(logic, t, 1.5, 0.05)
    t, status = feed(logic, t, 2.0, 0.9)
    check('2 次时升级为轻度', status.fatigue == FATIGUE_MILD,
          '{} / count={}'.format(status.fatigue, status.yawn_count))

    for _ in range(2):
        t, status = feed(logic, t, 1.5, 0.05)
        t, status = feed(logic, t, 2.0, 0.9)
    check('4 次时升级为重度', status.fatigue == FATIGUE_SEVERE,
          '{} / count={}'.format(status.fatigue, status.yawn_count))


def case_tolerate_blink() -> None:
    """短暂漏检（<1s）不应打断「张嘴中」的连续性。"""
    print('\n=== 用例 6：瞬时漏检不打断 ===')
    logic = YawnLogic(no_face_sec=1.0)
    t = 0.0
    t, status = feed(logic, t, 1.0, 0.9)                        # 张嘴 1s
    t, status = feed(logic, t, 0.3, 0.0, has_face=False)         # 丢脸 0.3s
    t, status = feed(logic, t, 1.0, 0.9)                        # 再张嘴 1s
    check('跨过 0.3s 漏检仍能计一次', status.yawn_count == 1, str(status.yawn_count))


def case_dip_grace() -> None:
    """概率短暂跌回阈值以下，不应把「连续张嘴」的计时清零。

    这是真机上的主要漏检原因：7fps 下 1.5 秒要有 10 帧全部达标，
    中间一帧因为人脸框抖动掉到阈值以下，整个哈欠就白打了。
    """
    print('\n=== 用例 7：概率瞬时跌落不打断 ===')
    logic = YawnLogic(threshold=0.5, yawn_min_sec=1.5, dip_grace_sec=0.3)
    t = 0.0
    t, status = feed(logic, t, 0.6, 0.9)                        # 张嘴 0.6s
    t, status = feed(logic, t, 0.2, 0.1)                        # 掉到 0.1，持续 0.2s（<0.3 容忍）
    check('跌落期间状态仍为张嘴中', status.state == STATE_OPEN, status.state)
    check('跌落期间计时未清零', status.open_duration >= 0.6,
          'open_duration={}'.format(status.open_duration))
    t, status = feed(logic, t, 1.0, 0.9)                        # 恢复张嘴
    check('恢复后仍能凑满并计一次', status.yawn_count == 1, str(status.yawn_count))

    # 反向：跌得太久（超过容忍窗口）仍应清零，不能被说话蒙混
    logic2 = YawnLogic(threshold=0.5, yawn_min_sec=1.5, dip_grace_sec=0.3)
    t = 0.0
    t, status = feed(logic2, t, 0.6, 0.9)
    t, status = feed(logic2, t, 0.8, 0.1)                       # 掉落 0.8s > 0.3
    check('长时间掉落会清零计时', status.open_duration == 0.0,
          'open_duration={}'.format(status.open_duration))
    t, status = feed(logic2, t, 1.0, 0.9)
    check('清零后重新计时，不会误计', status.yawn_count == 0, str(status.yawn_count))


def main() -> int:
    print('=== 打哈欠时序状态机自检（10Hz 喂帧）===')
    case_continuous_yawn()
    case_short_flash()
    case_cooldown()
    case_no_face()
    case_fatigue_level()
    case_tolerate_blink()
    case_dip_grace()

    failed = [name for name, ok, _detail in _results if not ok]
    print('\n=== 汇总 ===')
    print('  共 {} 项，通过 {} 项，失败 {} 项'.format(
        len(_results), len(_results) - len(failed), len(failed)))
    if failed:
        for name in failed:
            print('  [失败] ' + name)
        return 1
    print('\n[通过] 时序状态机行为符合预期')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
