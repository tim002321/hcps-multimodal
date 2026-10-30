# -*- coding: utf-8 -*-
"""差分测试：新的 A2 分析层 与 老工程 load_analyze 逐值对比。

用同一个合成会话库喂两边，比较：

1. 心电特征表  新 EcgProcessor            vs 老 ecg_process.Ecg_Compute.get_feature
2. 眼动特征表  新 EyeProcessor            vs 老 ecg_process.get_eye_feature
3. 10 维特征表 新 build_features          vs 老 ecg_process.get_input
4. 负荷等级    新 analyze / LoadAssessment vs 老 load_assessment.load_assessment_function

老代码认的是 load_analyze/ 那一份（界面就是 `from load_analyze.ecg_process import get_input`），
根目录同名文件是零引用重复件，这里显式把 load_analyze 放在 sys.path 最前面。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

OLD_ROOT = r'F:\hcpsproject\PyCharmMiscProject'
OLD_ANALYZE = os.path.join(OLD_ROOT, 'load_analyze')
NEW_ROOT = r'F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp'

sys.path.insert(0, NEW_ROOT)
sys.path.insert(0, OLD_ROOT)
sys.path.insert(0, OLD_ANALYZE)          # 让 `import ecg_process` 命中 load_analyze 那份

from ecg_process import Ecg_Compute, get_eye_feature, get_input      # noqa: E402  老实现
from load_assessment import load_assessment_function                 # noqa: E402  老实现

from core.A2_analyze import EcgProcessor, EyeProcessor, analyze, build_features   # noqa: E402
from tools.synth_session import build_session                                     # noqa: E402

SECONDS = 120.0
ECG_CONFIG = {'sample_rate': 240, 'window_size': 60, 'overlap': 0.8}
EYE_CONFIG = {'window_size': 60, 'overlap': 0.8}
MODELS = ['SVM', 'CatBoost', 'XGBoost']


def frame_diff(name: str, new: pd.DataFrame, old: pd.DataFrame) -> str:
    """比较两张表：列名、行数、数值最大偏差。返回空串表示一致。"""
    if list(new.columns) != list(old.columns):
        return f'{name} 列不一致：新 {list(new.columns)} vs 老 {list(old.columns)}'
    if len(new) != len(old):
        return f'{name} 行数不一致：新 {len(new)} vs 老 {len(old)}'
    worst = 0.0
    for column in new.columns:
        sa, sb = new[column], old[column]
        if pd.api.types.is_numeric_dtype(sa) and pd.api.types.is_numeric_dtype(sb):
            a = np.asarray(sa, dtype=float)
            b = np.asarray(sb, dtype=float)
            both_nan = np.isnan(a) & np.isnan(b)
            diff = np.abs(np.where(both_nan, 0.0, a - b))
            diff = diff[~np.isnan(diff)]
            if len(diff):
                worst = max(worst, float(np.max(diff)))
        elif not sa.reset_index(drop=True).astype(str).equals(sb.reset_index(drop=True).astype(str)):
            return f'{name} 列 {column} 不一致'
    print(f'  {name}: {len(new)} 行 {len(new.columns)} 列，最大数值偏差 {worst:.3e}')
    return '' if worst < 1e-9 else f'{name} 数值偏差过大：{worst:.3e}'


def main() -> int:
    print(f'造 {SECONDS:.0f} 秒合成会话数据…')
    session = build_session(seconds=SECONDS, task='analyze_regress', fresh=True)
    db_path, start, end = session['db_path'], session['start'], session['end']
    print(f'会话库：{db_path}\n')

    print('=== 新实现 ===')
    ecg_new = EcgProcessor(ECG_CONFIG).analyze(db_path, start, end)
    eye_new = EyeProcessor(EYE_CONFIG).analyze(db_path, start, end)
    features_new = build_features(db_path, start, end, {'ecg': ECG_CONFIG, 'eye': EYE_CONFIG})
    preds_new = analyze(db_path, start, end, models=MODELS,
                        config={'ecg': ECG_CONFIG, 'eye': EYE_CONFIG})
    print(f'  心电 {len(ecg_new)} 行 / 眼动 {len(eye_new)} 行 / 特征 {len(features_new)} 行 '
          f'/ 预测 {len(preds_new)} 行')

    print('\n=== 老实现（load_analyze）===')
    ecg_old = Ecg_Compute().get_feature(db_path, start, end, 240, 60, 0.8)
    eye_old = get_eye_feature(db_path, start, end, 240, 60, 0.8)
    features_old = get_input(db_path, start, end)
    if features_old is None:
        print('[失败] 老实现 get_input 返回 None（它自己的异常日志里有原因）')
        return 1

    cwd = os.getcwd()
    try:
        os.chdir(OLD_ROOT)               # 老 load_models 里是 'saved_models/xxx.pkl' 相对路径
        preds_old = load_assessment_function(features_old, MODELS)
    finally:
        os.chdir(cwd)
    print(f'  心电 {len(ecg_old)} 行 / 眼动 {len(eye_old)} 行 / 特征 {len(features_old)} 行 '
          f'/ 预测 {len(preds_old)} 行')

    print('\n=== 逐项比对 ===')
    problems = []
    problems.append(frame_diff('心电特征', ecg_new, ecg_old))
    problems.append(frame_diff('眼动特征', eye_new, eye_old))
    problems.append(frame_diff('10 维特征', features_new, features_old))

    if list(preds_new.columns) != list(preds_old.columns):
        problems.append(f'预测表列不一致：新 {list(preds_new.columns)} vs 老 {list(preds_old.columns)}')
    else:
        same = True
        for column in preds_old.columns:
            if not preds_new[column].reset_index(drop=True).equals(preds_old[column].reset_index(drop=True)):
                same = False
                print(f'  预测列 {column} 不一致：')
                print(f'    新 {list(preds_new[column])[:8]}')
                print(f'    老 {list(preds_old[column])[:8]}')
        print(f'  负荷等级: {len(preds_new)} 行，三模型预测{"完全一致" if same else "不一致"}')
        if not same:
            problems.append('负荷等级预测不一致')

    problems = [problem for problem in problems if problem]
    if problems:
        print('\n[失败] ' + '；'.join(problems))
        return 1
    print('\n[通过] 新 A2 分析层与老 load_analyze 逐值一致')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
