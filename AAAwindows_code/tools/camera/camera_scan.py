#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描本机可用的 USB 摄像头（cv2 索引 + 实际分辨率 + 画面亮度）。

用法：
    python tools/camera_scan.py            # 扫 0~9 号
    python tools/camera_scan.py --max 4

亮度均值能区分「摄像头打开了但没画面」（全黑时接近 0）和正常画面。
"""
from __future__ import annotations

import argparse

import cv2


def main() -> int:
    parser = argparse.ArgumentParser(description='扫描可用摄像头')
    parser.add_argument('--max', type=int, default=10, help='扫描到第几号（不含）')
    parser.add_argument('--backend', default='dshow', choices=['dshow', 'msmf', 'any'])
    args = parser.parse_args()

    backends = {'dshow': cv2.CAP_DSHOW, 'msmf': cv2.CAP_MSMF, 'any': cv2.CAP_ANY}
    backend = backends[args.backend]
    print('OpenCV {}  后端 {}'.format(cv2.__version__, args.backend))
    found = 0
    for index in range(max(0, args.max)):
        capture = cv2.VideoCapture(index, backend)
        if not capture.isOpened():
            capture.release()
            continue
        for _ in range(6):                             # 丢掉预热帧
            capture.read()
        ok, frame = capture.read()
        if ok and frame is not None:
            found += 1
            print('  index {}  OK  {}x{}  亮度均值 {:.1f}'.format(
                index, frame.shape[1], frame.shape[0], float(frame.mean())))
        else:
            print('  index {}  打开了但读不到帧'.format(index))
        capture.release()
    if not found:
        print('没有找到可用摄像头')
        return 1
    print('共 {} 个可用摄像头'.format(found))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
