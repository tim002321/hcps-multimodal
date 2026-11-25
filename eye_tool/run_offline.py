# -*- coding: utf-8 -*-
"""离线跑：图片文件夹 / 图片 / 视频文件 → CSV + 摘要 + 标注图。

用法：
    python run_offline.py --input <目录或文件> [--out 输出目录] [--limit N]
                          [--save-frames 5] [--fps 30]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eyetool.capture import ImageFolderCapture, VideoFileCapture    # noqa: E402
from eyetool.params import Params                                  # noqa: E402
from eyetool.pipeline import EyePipeline, annotate                 # noqa: E402


def build_source(args, params):
    path = args.input
    if os.path.isdir(path):
        return ImageFolderCapture(path, gray=args.gray, recursive=not args.no_recursive)
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png', '.bmp'):
        return ImageFolderCapture(path, gray=args.gray)
    cap = VideoFileCapture(path, gray=args.gray)
    if args.fps:
        cap.fps = float(args.fps)
    return cap


def main():
    ap = argparse.ArgumentParser(description='眼动分析（离线）')
    ap.add_argument('--input', required=True, help='图片目录 / 图片 / 视频')
    ap.add_argument('--out', default='', help='输出目录，默认 out/<输入名>_<时间>')
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 帧')
    ap.add_argument('--save-frames', type=int, default=3, help='保存几张标注图')
    ap.add_argument('--fps', type=float, default=0, help='视频源没有正确帧率时手动指定')
    ap.add_argument('--provider', default='auto', choices=('auto', 'cuda', 'cpu'))
    ap.add_argument('--no-recursive', action='store_true')
    ap.add_argument('--gray', action='store_true', help='输出灰度图（默认彩色）')
    args = ap.parse_args()

    params = Params().resolve_paths(HERE)
    params.detect.provider = args.provider
    params.detect.providers = None
    params.resolve_paths(HERE)
    if not params.detect.face_model:
        raise SystemExit('找不到 det_500m.onnx，放到 eye_tool/models/ 或主程序 models/ 下')

    out_dir = args.out or os.path.join(
        HERE, 'out', '%s_%s' % (os.path.basename(os.path.normpath(args.input)),
                                time.strftime('%Y%m%d_%H%M%S')))
    os.makedirs(out_dir, exist_ok=True)
    frames_dir = os.path.join(out_dir, 'annotated')
    if args.save_frames:
        os.makedirs(frames_dir, exist_ok=True)

    src = build_source(args, params).start()
    pipe = EyePipeline(params)
    print('后端: %s  输入: %s  帧数: %s' % (pipe.detector.active_provider,
                                        args.input,
                                        getattr(src, 'files', ['?']) and len(getattr(src, 'files', []))
                                        or '视频'))
    csv_path = os.path.join(out_dir, 'frames.csv')
    saved = 0
    last_res = None
    # 模型首次推理要建图（FaceMesh 的 tflite 图初始化要几秒），
    # 把第一帧算进来会让"帧/秒"低一个数量级，所以从第二帧开始计时
    t_start = None
    proc_ms = 0.0
    n = 0
    rows = []

    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['frame', 't_sec', 'face_ok', 'eye_w_px',
                    'd_a', 'conf_a', 'd_b', 'conf_b', 'd_mean',
                    'baseline_px', 'delta_pct', 'closed',
                    'blinks', 'bpm'])
        while True:
            frame, idx, t = src.read()
            if frame is None:
                break
            n += 1
            if t_start is None:
                t_first = time.perf_counter()
                t_start = None
            _t0 = time.perf_counter()
            res = pipe.process(frame, t if t else n / float(getattr(src, 'fps', 30.0)))
            if n > 1:
                proc_ms += (time.perf_counter() - _t0) * 1000.0
            last_res = res
            eyes = res['eyes']
            a = eyes.get('eye_a', {})
            b = eyes.get('eye_b', {})
            eye_w = max([v.get('eye_w', 0) for v in eyes.values()] or [0])
            w.writerow([idx, round(t, 4), int(res['face_ok']), eye_w,
                        round(a.get('d_px', float('nan')), 2) if a else '',
                        round(a.get('conf', 0), 3) if a else '',
                        round(b.get('d_px', float('nan')), 2) if b else '',
                        round(b.get('conf', 0), 3) if b else '',
                        round(res['d_mean'], 2) if np.isfinite(res['d_mean']) else '',
                        round(res['baseline'], 2) if np.isfinite(res['baseline']) else '',
                        round(res['delta_pct'], 2) if np.isfinite(res['delta_pct']) else '',
                        int(res['closed']), res['blinks'],
                        round(res['bpm'], 1) if np.isfinite(res['bpm']) else ''])
            if args.save_frames and saved < args.save_frames and idx % max(1, (args.limit or n or 50) // args.save_frames) == 1:
                vis = annotate(frame, res, [
                    'frame %d  t=%.2fs  %s' % (idx, t, pipe.detector.active_provider),
                    ('pupil %.2f mm  base %.2f  delta %+.1f%%' if res.get('unit') == 'mm'
                     else 'pupil %.1f px  base %.1f  delta %+.1f%%') % (
                        res['d_mean'], res['baseline'], res['delta_pct']),
                    'blinks %d  bpm %.1f  eye %d px' % (res['blinks'], res['bpm'], eye_w),
                ])
                cv2.imencode('.png', vis)[1].tofile(
                    os.path.join(frames_dir, 'frame_%06d.png' % idx))
                saved += 1
            if n == 1:
                t_start = time.perf_counter()      # 首帧只做预热，不计入速度
            if args.limit and n >= args.limit:
                break
    src.stop()

    elapsed = max(proc_ms, 1e-6) / 1000.0        # 只统计算法耗时，不含读盘/写 CSV
    fps_proc = max(0, n - 1) / elapsed if elapsed > 0 else 0
    valid = pipe.metrics.validity()
    unit = 'mm' if (last_res or {}).get('unit') == 'mm' else 'px'
    summary = [
        '输入        %s' % args.input,
        '帧数        %d' % n,
        '处理耗时    %.2f s（%.1f 帧/秒）' % (elapsed, fps_proc),
        '后端        %s' % pipe.detector.active_provider,
        '瞳孔检出率  %.1f%%' % (valid * 100),
        ('瞳孔/虹膜   中位 %.3f' % (last_res or {}).get('ratio_mean', float('nan'))
         if unit == 'mm' else '瞳孔/虹膜   --（阈值法没有这个指标）'),
        ('瞳孔直径    中位 %.2f mm（基线 %.2f mm）' if unit == 'mm'
         else '瞳孔直径    中位 %.2f px（基线 %.2f px）') % (
            float(np.median([d for _t, d in pipe.metrics.samples]))
            if len(pipe.metrics.samples) else float('nan'),
            pipe.metrics.baseline),
        '眨眼次数    %d' % pipe.blink.count,
        '眨眼频率    %.1f 次/分' % (pipe.blink.rate_per_min(n / float(getattr(src, 'fps', 30.0)))),
        '平均眨眼    %.0f ms' % pipe.blink.mean_duration_ms(),
        '闭眼占比    %.1f%%' % (pipe.metrics.closed_fraction() * 100),
        'CSV         %s' % csv_path,
    ]
    text = '\n'.join(summary)
    with open(os.path.join(out_dir, 'summary.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print()
    print(text)


if __name__ == '__main__':
    main()
