#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""眼动 UDP 联调工具：不开界面，直接看 aSeeStudio 发过来的数据能不能解析。

现场接眼动仪时会遇到的问题基本就三类，这个脚本对着一一排查：

  1. **收包 0** —— 端口对不上，或 aSeeStudio 还没点「开始转发」，
     或 Windows 防火墙拦了 UDP。先看 `receiver=0`。
  2. **收包有、解析失败多** —— 报文形态和预期不一致（不是 JSON 数组也不是
     逗号分隔）。加 `--raw` 把原始串打出来，一眼能看出是什么格式。
  3. **解析成功但值明显不对** —— 字段顺序和文档不一致。看每帧打印的那行
     摘要（综合坐标 / 瞳孔 / AOI）和 aSeeStudio 界面上显示的数值对比。

用法：

    # 只看解析结果（默认端口 8003，跑 10 秒）
    python tools/eye/listen_eye.py --seconds 10

    # 同时落库，验证写出来的 JSON：库在临时目录，不污染 data/
    python tools/eye/listen_eye.py --save --seconds 10

    # 字段对不上时，把原始串原样打出来
    python tools/eye/listen_eye.py --raw --seconds 5

配合 `fake_eye_sender.py` 可以在没有设备的情况下把整条链路跑通。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from devices.eye_protocol import parse_asee, to_text        # noqa: E402
from devices.eye_receiver import EyeReceiver               # noqa: E402


def _build_recorder(db_dir: str):
    """按需建一个只写眼动的 recorder；不落库时返回 None。"""
    from core.db_storage import DataRecorder, SensorDB

    os.makedirs(db_dir, exist_ok=True)
    storage = SensorDB(data_dir=db_dir, db_name='eye_probe_{}.db'.format(
        time.strftime('%Y%m%d_%H%M%S')))
    print('[联调] 落库到 ' + storage.db_path, flush=True)
    return DataRecorder(storage), storage


def main() -> int:
    parser = argparse.ArgumentParser(description='眼动 UDP 联调：打印解析结果与统计')
    parser.add_argument('--port', type=int, default=8003, help='监听端口（默认 8003）')
    parser.add_argument('--bind', default='0.0.0.0', help='监听地址（默认 0.0.0.0）')
    parser.add_argument('--seconds', type=float, default=10.0, help='跑多久（0 = 一直跑）')
    parser.add_argument('--save', action='store_true', help='同时写库，验证入库链路')
    parser.add_argument('--db-dir', default='', help='库放哪（默认系统临时目录下的 eye_probe/）')
    parser.add_argument('--raw', action='store_true', help='每帧打印原始报文')
    parser.add_argument('--quiet', action='store_true', help='不打印每帧摘要，只打统计')
    args = parser.parse_args()

    recorder = storage = None
    if args.save:
        db_dir = args.db_dir or os.path.join(os.environ.get('TEMP', '/tmp'), 'eye_probe')
        recorder, storage = _build_recorder(db_dir)

    receiver = EyeReceiver(recorder=recorder, port=args.port, bind_host=args.bind,
                           debug=not args.quiet, idle_warn_sec=5.0)
    receiver.start()
    started = time.time()
    print('[联调] 监听 {}:{}，Ctrl+C 结束'.format(args.bind, args.port), flush=True)

    try:
        while args.seconds <= 0 or time.time() - started < args.seconds:
            time.sleep(1.0)
            stats = receiver.stats()
            print('[统计] {:.0f}s 收包={} 解析={} 失败={} 入库={} 帧率={:.1f}/s 来源={}'.format(
                time.time() - started, stats['received'], stats['parsed'],
                stats['failed'], stats['written'], stats['rate'], stats['peer'] or '—'),
                flush=True)
            if not stats['bound']:
                break
    except KeyboardInterrupt:
        print('\n[联调] 手动结束', flush=True)
    finally:
        receiver.stop()

    stats = receiver.stats()
    print()
    print('===== 汇总 =====')
    print('端口绑定   ', '成功' if stats['listen_ok'] else '失败')
    print('收包 / 解析 / 失败 / 入库',
          '{} / {} / {} / {}'.format(stats['received'], stats['parsed'],
                                    stats['failed'], stats['written']))
    print('平均帧率   ', '{:.1f} 帧/秒'.format(stats['rate']))
    print('来源地址   ', stats['peer'] or '—')
    print('最后时间戳 ', stats['last_timestamp'])
    print('最后摘要   ', stats['last_summary'] or '—')
    print('最后 AOI   ', '/'.join(stats['last_aois']) if stats['last_aois'] else '（空）')
    if stats['last_error']:
        print('最后错误   ', stats['last_error'])

    frame = receiver.latest()
    if frame is not None and args.raw:
        print()
        print('===== 原始报文 =====')
        print(to_text(frame.raw))
    elif frame is not None:
        # 不落库时也把这一帧按字段铺开，便于和 aSeeStudio 界面逐项对
        print()
        print('===== 最后一帧字段 =====')
        for key, value in frame.as_row().items():
            print('  {:14s} {}'.format(key, value))

    if stats['received'] == 0:
        print()
        print('提示：一个包都没收到。依次确认 ①aSeeStudio 的转发目标 IP/端口 '
              '②Windows 防火墙放行 UDP {} ③目标地址是否填的本机 IP'.format(args.port))
    elif stats['failed'] and not stats['parsed']:
        print()
        print('提示：包收到了但全部解析失败，加 --raw 看原始串是什么形态。')

    # 兜底：解析一帧独立报文，确认解析器本身没问题（与收包无关）
    if stats['received'] == 0 and args.save:
        print()
        print('（同时做一次离线自检）')
        sample = ('[1782367662834217, true, 0.645575, 1.0662, true, 0.618701, '
                  '0.984101, true, 0.672449, 1.1483, 3.51215, 2.34143, 0, 0, []]')
        parsed = parse_asee(sample)
        print('  样例解析：', parsed.summary() if parsed else '失败')

    if storage is not None:
        storage.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
