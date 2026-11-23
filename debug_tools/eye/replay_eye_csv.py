# -*- coding: utf-8 -*-
"""把录下来的眼动 CSV 当"录音带"，在本机重放成 TCP 数据流。

用途：设备不在手边时验证接收端 / 分帧 / 解析 / 落盘。
CSV 必须是 receive_tcp_5001.py 产出的 27 列格式（本机时间、设备时间(ms) + 25 个原始字段），
这样能原样还原成设备当时发出的 JSON 报文，连字段值和相对节奏都不变。

用法：
    python replay_eye_csv.py 眼动数据_7901.csv -p 17901 --speed 8
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
import sys
import threading
import time


def load_frames(path: str):
    """读 CSV → [(设备毫秒时间, 原始 JSON 字节)]"""
    with io.open(path, encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise SystemExit('CSV 是空的：{}'.format(path))
    header = rows[0]
    # 前两列是宿主时间与设备时间，其余列名就是设备原始键名
    keys = header[2:]
    frames = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        try:
            dev_ms = int(float(row[1]))
        except ValueError:
            continue
        obj = {'timestamp': dev_ms}
        for key, cell in zip(keys, row[2:]):
            if cell == '':
                obj[key] = None
                continue
            try:
                number = float(cell)
                obj[key] = int(number) if number.is_integer() and '.' not in cell else number
            except ValueError:
                obj[key] = cell
        frames.append((dev_ms, json.dumps(obj, ensure_ascii=False,
                                          separators=(',', ':')).encode('utf-8')))
    frames.sort(key=lambda item: item[0])
    return frames


def serve(frames, port: int, speed: float, bind: str = '127.0.0.1') -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(1)
    srv.settimeout(20)
    print('[重放] 监听 {}:{}，等待接收端连入…'.format(bind, port), flush=True)
    try:
        conn, peer = srv.accept()
    except socket.timeout:
        print('[重放] 20 秒没人连进来，退出')
        srv.close()
        return 0
    print('[重放] {} 已连入，开始发送 {} 帧（{:.0f} 倍速）'.format(
        peer, len(frames), speed), flush=True)

    base_dev = frames[0][0]
    t0 = time.time()
    sent = 0
    for dev_ms, payload in frames:
        due = (dev_ms - base_dev) / 1000.0 / max(speed, 0.01)
        wait = due - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)
        try:
            conn.sendall(payload)
        except OSError as exc:
            print('[重放] 发送中断：{}（已发 {} 帧）'.format(exc, sent))
            break
        sent += 1
    try:
        conn.shutdown(socket.SHUT_WR)     # 发完关写端，让接收端读到 EOF
        time.sleep(0.3)
        conn.close()
    except OSError:
        pass
    srv.close()
    print('[重放] 发送完成 {} 帧，用时 {:.1f} 秒'.format(sent, time.time() - t0), flush=True)
    return sent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='把眼动 CSV 重放成本机 TCP 数据流')
    ap.add_argument('csv')
    ap.add_argument('-p', '--port', type=int, default=17901)
    ap.add_argument('--bind', default='127.0.0.1')
    ap.add_argument('--speed', type=float, default=1.0, help='重放倍速，默认 1.0（按原始节奏）')
    ap.add_argument('--limit', type=int, default=0, help='只重放前 N 帧，0=全部')
    args = ap.parse_args(argv)

    if not os.path.exists(args.csv):
        print('找不到 CSV：{}'.format(args.csv))
        return 2
    frames = load_frames(args.csv)
    if args.limit:
        frames = frames[:args.limit]
    span = (frames[-1][0] - frames[0][0]) / 1000.0
    print('[重放] {} 帧，原始跨度 {:.1f} 秒，字段 {} 个'.format(
        len(frames), span, len(frames[0][1]) and frames[0][1].count(b':')))
    serve(frames, args.port, args.speed, args.bind)
    return 0


if __name__ == '__main__':
    sys.exit(main())
