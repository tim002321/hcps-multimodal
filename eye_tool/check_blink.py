# -*- coding: utf-8 -*-
"""眨眼计数对拍：录 30 秒，把程序数出来的次数和你自己数的对比。

用法（在 eye_tool 目录下）：

    ..\\shifeiyuantwo\\Scripts\\python.exe check_blink.py
    ..\\shifeiyuantwo\\Scripts\\python.exe check_blink.py --seconds 60
    ..\\shifeiyuantwo\\Scripts\\python.exe check_blink.py --expect 10   # 你打算眨 10 次

流程：
  1. 等画面里出现人脸（最多 15 秒）
  2. 提示"开始"后录制 N 秒，屏幕上会实时显示已数到的次数
  3. 结束后打印：次数、频率、闭眼占比、长闭眼次数、每次的时长
     并生成 CSV 和开合比曲线图，方便逐帧核对

注意：**必须用项目 venv 的 python**（需要 mediapipe / onnxruntime）。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eyetool.capture import FfmpegCapture          # noqa: E402
from eyetool.params import Params                  # noqa: E402
from eyetool.pipeline import EyePipeline           # noqa: E402
from eyetool.scrfd import Scrfd                    # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=30.0, help='录制时长')
    ap.add_argument('--expect', type=int, default=0,
                    help='你打算眨几次（自然眨眼就不用填）')
    ap.add_argument('--out', default=os.path.join(HERE, 'out', 'blink'))
    ap.add_argument('--provider', default='auto', choices=('auto', 'cuda', 'cpu'))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    params = Params()
    params.detect.provider = args.provider
    params = params.resolve_paths(HERE)

    print('[1/4] 打开摄像头…')
    cap = FfmpegCapture(params.capture).start()
    det = Scrfd(params.detect.face_model, conf=0.4, providers=params.detect.providers)
    print('      设备：%s  %dx%d' % (cap.device, params.capture.width, params.capture.height))

    print('[2/4] 等你出现在画面里（最多 15 秒）…')
    t0 = time.time()
    while time.time() - t0 < 15:
        frame, _i, _t = cap.read(0.5)
        if frame is None:
            continue
        if det.detect(frame):
            break
    else:
        cap.stop()
        raise SystemExit('画面里一直没有人脸，检查镜头方向和机位')

    print('[3/4] 开始录制 %.0f 秒。请正对镜头、正常看屏幕；' % args.seconds)
    if args.expect:
        print('      你计划眨 %d 次，结束后我会对比。' % args.expect)
    print('      倒计时 3 秒…')
    for k in (3, 2, 1):
        print('        %d' % k, flush=True)
        time.sleep(1.0)

    pipe = EyePipeline(params)
    rows = []
    events = []
    closed_frames = 0
    t_start = time.time()
    last_print = 0.0
    while time.time() - t_start < args.seconds:
        frame, _i, dev_t = cap.read(0.4)
        if frame is None:
            continue
        t = time.time() - t_start
        res = pipe.process(frame, t)
        eyes = res['eyes']
        op = [v.get('open_ratio') for v in eyes.values()
              if v.get('open_ratio') is not None]
        closed_frames += 1 if res['closed'] else 0
        rows.append([round(t, 3),
                     round(float(min(op)), 3) if op else '',
                     int(res['face_ok']), int(res['closed']),
                     res['blinks'],
                     round(res['d_mean'], 2) if np.isfinite(res['d_mean']) else ''])
        if res.get('blink_event') is not None:
            events.append(res['blink_event'])
        # 每秒刷一次进度，方便你边录边看
        if t - last_print >= 1.0:
            last_print = t
            print('       %2.0fs  已数到 %d 次' % (t, res['blinks']), flush=True)
    cap.stop()

    dur = max(1e-6, rows[-1][0] - rows[0][0])
    stamp = time.strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(args.out, 'blink_%s.csv' % stamp)
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['t', 'open_ratio', 'face', 'closed', 'blinks', 'd_mm'])
        w.writerows(rows)

    n = len(events)
    print('\n[4/4] 结果')
    print('  录制时长      %.1f s（%d 帧）' % (dur, len(rows)))
    print('  眨眼次数      %d 次' % n)
    print('  眨眼频率      %.1f 次/分' % (n * 60.0 / dur))
    if n:
        durs = [e.duration_ms for e in events]
        print('  单次时长      中位 %.0f ms（%.0f~%.0f）'
              % (np.median(durs), min(durs), max(durs)))
    print('  闭眼帧占比    %.1f%%' % (100.0 * closed_frames / max(1, len(rows))))
    print('  长闭眼(>0.5s) %d 次' % pipe.blink.long_closures)
    if args.expect:
        diff = n - args.expect
        print('  你计划        %d 次 → 差 %+d 次' % (args.expect, diff))
    print('  CSV           %s' % csv_path)

    # 曲线图，便于逐帧核对
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
        plt.rcParams['axes.unicode_minus'] = False
        t = np.array([r[0] for r in rows])
        op = np.array([float(r[1]) if r[1] != '' else np.nan for r in rows])
        cl = np.array([r[3] for r in rows])
        base = np.nanpercentile(op, 90) if np.isfinite(op).any() else 0
        fig, ax = plt.subplots(figsize=(14, 4))
        ax.plot(t, op, lw=1.0, color='#185FA5', label='眼睑开合比')
        ax.axhline(base, color='#888', ls='--', lw=0.8, label='基线 %.3f' % base)
        ax.axhline(0.45 * base, color='#C0392B', ls=':', lw=1.0, label='进入闭眼 0.45×基线')
        ax.fill_between(t, 0, 0.5, where=cl.astype(bool), color='#F5B7B1',
                        alpha=0.55, step='mid', label='判定为闭眼')
        for e in events:
            ax.axvline(e.start, color='#1E8449', lw=0.8, alpha=0.7)
        ax.set_xlim(0, t[-1]); ax.set_ylim(0, max(0.5, np.nanmax(op) * 1.1))
        ax.set_xlabel('时间 (s)'); ax.set_ylabel('开合比')
        ax.set_title('眨眼对拍：红区=判定闭眼，绿线=记一次眨眼')
        ax.legend(loc='upper right', fontsize=8); ax.grid(alpha=0.25)
        plt.tight_layout()
        png = os.path.join(args.out, 'blink_%s.png' % stamp)
        plt.savefig(png, dpi=120)
        print('  曲线图        %s' % png)
    except Exception as exc:                            # noqa: BLE001
        print('  （曲线图没画出来：%s）' % exc)


if __name__ == '__main__':
    main()
