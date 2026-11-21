#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假眼动发送端：没有 aSeeStudio 时用来打通整条链路。

按 aSeeStudio 实测报文形态造帧（JSON 数组、15 项、时间戳微秒、AOI 为数组），
发到指定 UDP 端口。用于验证：

    python tools/eye/fake_eye_sender.py --seconds 5
    python tools/eye/listen_eye.py --save --seconds 8      # 另一个窗口

造出来的数据故意带几种情况，好让下游的容错分支都被走到：
  * 每秒有 1 帧综合注视无效（坐标为 null）
  * 每 2 秒切换一次 AOI（空 / 单个 / 两个）
  * 事件字段偶尔置 1
  * 瞳孔左右眼有固定差值（模拟真实两眼不一致）

**不要拿它当性能压测工具**：它是纯 Python 单线程 sendto，60Hz 足够，
再高会开始丢时间精度。
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import time


def build_frame(index: int, hz: float, start_ns: int) -> list:
    """造第 index 帧。字段顺序严格对齐 aSeeStudio 实测样本。"""
    t = index / max(hz, 0.1)
    # 注视点：绕中心缓慢漂移，Y 偶尔越过 1.0（模拟视线出屏）
    x = 0.5 + 0.15 * math.sin(t * 1.3)
    y = 0.5 + 0.55 * math.sin(t * 0.7)
    valid = not (index % int(max(hz, 1)) == 0)          # 每秒 1 帧无效
    left_pupil = 3.50 + 0.15 * math.sin(t * 2.1)
    right_pupil = 2.34 + 0.12 * math.sin(t * 1.7 + 0.6)

    aoi_cycle = int(t / 2) % 3
    aoi = ([], ['中心区'], ['左侧区域', '右侧区域'])[aoi_cycle]

    return [
        start_ns // 1000 + int(t * 1e6),                # 时间戳（微秒）
        valid,                                          # 综合注视是否有效
        round(x, 6) if valid else None,                 # 综合 X
        round(y, 6) if valid else None,                 # 综合 Y
        valid,                                          # 左眼是否有效
        round(x - 0.02, 6) if valid else None,
        round(y - 0.03, 6) if valid else None,
        valid,                                          # 右眼是否有效
        round(x + 0.02, 6) if valid else None,
        round(y + 0.03, 6) if valid else None,
        round(left_pupil, 5),                           # 左眼瞳孔 mm
        round(right_pupil, 5),                          # 右眼瞳孔 mm
        1 if index % int(max(hz * 2, 1)) == 0 else 0,   # 左眼事件
        0,                                              # 右眼事件
        aoi,                                            # AOI 名称数组
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description='假眼动发送端')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8003)
    parser.add_argument('--seconds', type=float, default=5.0, help='发多久')
    parser.add_argument('--hz', type=float, default=60.0, help='帧率（默认 60）')
    parser.add_argument('--count', type=int, default=0, help='只发 N 帧（优先于 --seconds）')
    args = parser.parse_args()

    total = args.count or max(1, int(args.seconds * args.hz))
    interval = 1.0 / max(args.hz, 0.1)
    start_ns = time.time_ns()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    next_at = time.perf_counter()

    print('[假眼动] -> {}:{}  {} 帧 @ {:.0f}Hz'.format(
        args.host, args.port, total, args.hz), flush=True)
    try:
        for index in range(total):
            payload = json.dumps(build_frame(index, args.hz, start_ns),
                                 ensure_ascii=False, separators=(',', ' '))
            try:
                sock.sendto(payload.encode('utf-8'), (args.host, args.port))
                sent += 1
            except OSError as exc:
                print('[假眼动] 发送失败：{}'.format(exc), flush=True)
                break
            next_at += interval
            sleep = next_at - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        print('\n[假眼动] 手动结束', flush=True)
    finally:
        sock.close()

    print('[假眼动] 已发送 {} 帧'.format(sent), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
