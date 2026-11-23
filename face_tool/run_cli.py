# -*- coding: utf-8 -*-
"""命令行入口。

例：
    python run_cli.py --scan "D:\\data\\sub-014\\1_20261117_192737"
    python run_cli.py "D:\\data\\...\\1_20261117_192737" -o "D:\\out"
    python run_cli.py "..." -o "D:\\out" --limit 20 --backend dlib
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from facetool.tv_stub import install as _install_tv_stub
    _install_tv_stub()
except Exception:  # noqa: BLE001
    pass

from facetool import report as report_mod              # noqa: E402
from facetool.batch import SUMMARY_NAME, BatchRunner     # noqa: E402
from facetool.output import OutputLayout               # noqa: E402
from facetool.params import RunParams                  # noqa: E402
from facetool.pipeline import Analyzer                 # noqa: E402
from facetool.paths import is_within                        # noqa: E402
from facetool.session import (discover_session, discover_sessions,  # noqa: E402
                              looks_like_session)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='run_cli.py',
        description='面部 ROI 离线处理：把一个会话目录处理成眼部/嘴部 ROI + 68 点标注 + 指标 CSV')
    p.add_argument('session', nargs='?', help='会话目录（含 frames/ 与 .db）')
    p.add_argument('-o', '--out', default='', help='输出根目录')
    p.add_argument('--flat', action='store_true', help='直接写入输出目录，不套会话名子目录')
    p.add_argument('--scan', action='store_true', help='只扫描并打印会话信息，不处理')
    p.add_argument('--single', action='store_true',
                   help='强制按单会话处理（默认会自动判断是否为批量根目录）')
    p.add_argument('--max-depth', type=int, default=6,
                   help='批量递归的最大层数（默认 6）')
    p.add_argument('--backend', default='auto', choices=['auto', 'torch', 'onnx', 'dlib'])
    p.add_argument('--conf', type=float, default=0.35)
    p.add_argument('--imgsz', type=int, default=640)
    p.add_argument('--device', default='')
    p.add_argument('--face-pick', default='center', choices=['center', 'largest'])
    p.add_argument('--center-band', type=float, default=0.25)
    p.add_argument('--min-face-px', type=int, default=64)
    p.add_argument('--roi-mode', default='hybrid',
                   choices=['hybrid', 'face_ratio', 'landmark_box'])
    p.add_argument('--eye-w-ratio', type=float, default=0.40)
    p.add_argument('--eye-h-ratio', type=float, default=0.26)
    p.add_argument('--mouth-w-scale', type=float, default=1.8)
    p.add_argument('--mouth-h-scale', type=float, default=3.6)
    p.add_argument('--mouth-dy', type=float, default=0.12)
    p.add_argument('--roi-scale', type=float, default=1.0, choices=[1.0, 2.0, 3.0, 4.0])
    p.add_argument('--jpeg-quality', type=int, default=95)
    p.add_argument('--eye-format', default='png', choices=['png', 'jpg'],
                   help='眼部 ROI 存 png（无损，默认）还是 jpg')
    p.add_argument('--annotate', action='store_true',
                   help='额外生成 68 点自检图（原图副本，含人脸，勿外传；默认关闭）')
    p.add_argument('--no-index', action='store_true', help='自检图上不带点编号')
    p.add_argument('--overwrite', action='store_true', help='忽略已存在产物，全部重算')
    p.add_argument('--limit', type=int, default=0, help='只处理前 N 帧（调试用）')
    p.add_argument('--models-dir', default='')
    return p


def params_from_args(args) -> RunParams:
    return RunParams(
        backend=args.backend, conf=args.conf, imgsz=args.imgsz, device=args.device,
        face_pick=args.face_pick, center_band=args.center_band,
        min_face_px=args.min_face_px, roi_mode=args.roi_mode,
        eye_w_ratio=args.eye_w_ratio, eye_h_ratio=args.eye_h_ratio,
        mouth_w_scale=args.mouth_w_scale, mouth_h_scale=args.mouth_h_scale,
        mouth_dy=args.mouth_dy, roi_scale=float(args.roi_scale),
        jpeg_quality=args.jpeg_quality, eye_format=args.eye_format,
        annotate=bool(args.annotate),
        annotate_index=not args.no_index, overwrite=args.overwrite,
        limit=args.limit, models_dir=args.models_dir,
    )


def _print_session(info) -> None:
    print('会话：', info.session_dir)
    print('　帧目录：', info.frames_dir)
    print('　数据库：', info.db_path or '（无）')
    if info.probe is not None:
        print('　库结构：', info.probe.describe())
    print('　统计　：', info.describe())


def _run_single(args, info, params, out_root: str) -> int:
    layout = OutputLayout(out_root, info.name, params, flat=args.flat)
    print('　输出　：', layout.dir)
    layout.open(info={
        'session': info.session_dir, 'frames_dir': info.frames_dir,
        'db': info.db_path, 'total': info.total,
        'prefixes': {k: v for k, v in info.prefix_counts.items()},
    })
    analyzer = Analyzer(info, params, layout, on_log=lambda m: print('  ·', m))
    status = 'finished'
    try:
        stats = analyzer.run()
        if stats.cancelled:
            status = 'cancelled'
    except KeyboardInterrupt:
        analyzer.stop()
        stats = analyzer.stats
        status = 'cancelled'
    except Exception as exc:                       # noqa: BLE001
        print('[失败]', exc)
        layout.finalize('failed', {'error': str(exc)})
        return 1

    summary = report_mod.build_summary(info, stats, params, layout.dir,
                                       {'used': analyzer._chain.name,
                                        'requested': params.backend,
                                        'device': analyzer._chain.device},
                                       layout.written)
    layout.finalize(status, stats.as_dict(), summary)
    print('完成：', stats.as_dict())
    print('报告：', os.path.join(layout.dir, 'summary.txt'))
    return 0


def _run_batch(args, entries, params, out_root: str) -> int:
    print('批量：{} 个会话，约 {} 帧 → {}'.format(
        len(entries), sum(e.est_frames for e in entries), out_root))
    runner = BatchRunner(entries, params, out_root,
                         on_log=lambda m: print('  ·', m))
    try:
        stats = runner.run()
    except KeyboardInterrupt:
        runner.stop()
        stats = runner.stats
        stats.cancelled = True
    print('批量完成：', stats.as_dict())
    print('汇总：', os.path.join(out_root, SUMMARY_NAME))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.session:
        build_parser().print_help()
        return 2

    params = params_from_args(args)
    problems = params.problems()
    if problems:
        print('[参数错误] ' + '；'.join(problems))
        return 2

    out_root = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    root = args.session

    # 输出放进输入目录会导致"把自己的产物当输入再处理一遍"，直接拦掉
    if is_within(out_root, root):
        print('[错误] 输出目录不能放在输入目录里面：\n'
              '  输入 {}\n  输出 {}\n'
              '  产物里的 eyes_left / mouth 会被当成新会话再处理一次。\n'
              '  请换一个独立目录，例如 D:\\处理后'.format(
                  os.path.abspath(root), os.path.abspath(out_root)))
        return 2

    # 判定：选中目录本身是会话 → 单会话；否则递归找子会话
    if args.single or looks_like_session(root):
        info = discover_session(root)
        if info.problem:
            print('[错误]', info.problem)
            return 2
        if is_within(out_root, info.session_dir):
            print('[错误] 输出目录不能放在会话目录里面：{}'.format(info.session_dir))
            return 2
        _print_session(info)
        if args.scan:
            for prefix, count in sorted(info.prefix_counts.items()):
                print('　前缀 {}：{} 帧'.format(prefix, count))
            return 0
        return _run_single(args, info, params, out_root)

    entries = discover_sessions(root, max_depth=args.max_depth)
    if not entries:
        print('[错误] 这个目录下没找到会话（既没有 frames/ 也没有会话库）')
        return 2
    if args.scan:
        print('批量扫描：{} 个会话'.format(len(entries)))
        for e in entries:
            print('  %-6s %-46s 约 %5d 帧' % (e.kind, e.rel, e.est_frames))
        return 0
    return _run_batch(args, entries, params, out_root)


if __name__ == '__main__':
    sys.exit(main())
