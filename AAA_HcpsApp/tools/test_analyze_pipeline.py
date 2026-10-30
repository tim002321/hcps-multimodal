# -*- coding: utf-8 -*-
"""分析层流水线测试（不需要硬件）：
合成会话库 -> 心电 HRV 特征 + 眼动窗口特征 -> 10 维负荷特征 -> SVM/CatBoost/XGBoost。

用法：
    python tools/test_analyze_pipeline.py                      # 造 120 秒数据跑完整链路
    python tools/test_analyze_pipeline.py --seconds 90 --fresh # 换更短的时长 / 新会话目录
    python tools/test_analyze_pipeline.py --skip-models        # 只测特征，不加载模型

合成数据是 60 bpm 的心电 + 均值 3.2mm 的瞳孔，所以顺带校验算法本身：
算出来的 bpm 应该贴着 60、pupil 应该贴着 3.2。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A2_analyze import EcgProcessor, EyeProcessor, analyze, build_features      # noqa: E402
from tools.synth_session import build_session                                        # noqa: E402

ECG_COLUMNS = ['bpm', 'rmssd', 'vlf', 'lf', 'hf', 'lf/hf', 'sd1', 'sd2/sd1', 'timestamp']
EYE_COLUMNS = ['pupil', 'blink_rate']
FEATURE_COLUMNS = ['pupil', 'blink_rate', 'bpm', 'hf', 'lf/hf', 'lf', 'vlf', 'sd2/sd1', 'rmssd', 'sd1']


def load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit('缺少 PyYAML：pip install pyyaml')
    with open(path, encoding='utf-8') as fp:
        return yaml.safe_load(fp) or {}


class Checks:
    """攒一堆断言，最后统一报告。"""

    def __init__(self):
        self.failures = []

    def check(self, name: str, ok: bool, detail: str = '') -> bool:
        print(f'  {"[OK]" if ok else "[!!]"} {name}{"" if ok else "  " + detail}')
        if not ok:
            self.failures.append(f'{name} {detail}'.strip())
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description='A2 分析层流水线测试')
    parser.add_argument('--config', default=str(ROOT / 'config' / 'default.yaml'))
    parser.add_argument('--seconds', type=float, default=120.0, help='合成多少秒数据')
    parser.add_argument('--task', default='analyze')
    parser.add_argument('--fresh', action='store_true', help='强制新开一个会话目录')
    parser.add_argument('--skip-models', action='store_true', help='跳过模型预测（不装 CatBoost 时用）')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    config = (load_config(Path(args.config)).get('analyze') or {})
    checks = Checks()

    print(f'\n造 {args.seconds:.0f} 秒合成数据（240Hz 心电 + 20Hz 眼动）…')
    session = build_session(seconds=args.seconds, task=args.task, fresh=args.fresh)
    db_path = session['db_path']
    print(f'会话库：{db_path}')
    print(f'  心电 {session["ecg_packets"]} 包 / 眼动 {session["eye_packets"]} 包')

    started = time.time()
    ecg = EcgProcessor(config.get('ecg')).analyze(db_path, session['start'], session['end'])
    eye = EyeProcessor(config.get('eye')).analyze(db_path, session['start'], session['end'])
    features = build_features(db_path, session['start'], session['end'], config)
    print(f'\n特征计算完成，用时 {time.time() - started:.1f} 秒')

    print('\n=== 心电特征 ===')
    print(f'  {len(ecg)} 行，列 {list(ecg.columns)}')
    if len(ecg):
        row = ecg.iloc[0]
        print(f'  首行：bpm={row["bpm"]:.2f} rmssd={row["rmssd"]:.2f} hf={row["hf"]:.4f} '
              f'lf/hf={row["lf/hf"]:.4f} sd1={row["sd1"]:.2f} sd2/sd1={row["sd2/sd1"]:.3f}')

    print('\n=== 眼动特征 ===')
    print(f'  {len(eye)} 行，列 {list(eye.columns)}')
    if len(eye):
        print(f'  首行：pupil={eye.iloc[0]["pupil"]:.4f} blink_rate={eye.iloc[0]["blink_rate"]:.4f}')

    print('\n=== 10 维负荷特征 ===')
    print(f'  {len(features)} 行')
    if len(features):
        print('  ' + features.iloc[0][FEATURE_COLUMNS].to_string().replace('\n', '\n  '))

    results = None
    if not args.skip_models:
        started = time.time()
        results = analyze(db_path, session['start'], session['end'], config=config)
        print(f'\n=== 负荷等级（三模型）用时 {time.time() - started:.1f} 秒 ===')
        print(results.to_string(index=False))

    print('\n=== 断言 ===')
    checks.check('心电特征列与老库一致', list(ecg.columns) == ECG_COLUMNS, f'实际 {list(ecg.columns)}')
    checks.check('眼动特征列与老库一致', list(eye.columns) == EYE_COLUMNS, f'实际 {list(eye.columns)}')
    checks.check('心电特征非空', len(ecg) > 0)
    checks.check('眼动特征非空', len(eye) > 0)
    checks.check('10 维特征齐了', all(col in features.columns for col in FEATURE_COLUMNS))
    checks.check('对齐后行数 = 两张短表里更短的那张',
                 len(features) == min(len(ecg), len(eye)), f'{len(features)} vs {min(len(ecg), len(eye))}')
    if len(ecg):
        bpm_mean = float(ecg['bpm'].mean())
        checks.check('bpm 贴着合成值的 60（R 峰检出正常）', abs(bpm_mean - 60) < 5, f'实际 {bpm_mean:.2f}')
    if len(eye):
        pupil_mean = float(eye['pupil'].mean())
        checks.check('pupil 贴着合成值的 3.2', abs(pupil_mean - 3.2) < 0.15, f'实际 {pupil_mean:.4f}')
    if results is not None:
        checks.check('预测表含三个模型 + time',
                     list(results.columns) == ['SVM', 'CatBoost', 'XGBoost', 'time'],
                     f'实际 {list(results.columns)}')
        checks.check('预测行数与特征行数一致', len(results) == len(features))
        checks.check('预测结果无空值', not results.isnull().any().any())

    ok = not checks.failures
    print('\n[通过] 分析层流水线正常' if ok else f'\n[失败] {len(checks.failures)} 项没过：{checks.failures}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
