# -*- coding: utf-8 -*-
"""分析主线（A2）：一个会话库进，10 维特征 / 负荷等级出。

对应老工程的两步调用（load_analyze_window.py 就是这么串的）：

    features = get_input(db_path, start, end)                  # load_analyze/ecg_process.py
    results  = load_assessment_function(features, models)       # load_analyze/load_assessment.py

新版拆成三个明确的名字，UI/推理链路按需取用：

    build_features(db_path, start, end)   # 眼动窗口特征 + 心电 HRV 特征，按短的那个对齐
    assess(features, models=...)          # 10 维特征 -> SVM / CatBoost / XGBoost
    analyze(db_path, start, end, ...)     # 上面两步一条龙（分析界面直接调这个）

对齐规则照搬老代码 get_input：两张表行数不等时，**行数多的那张用
``sample(n=min_len, random_state=42)`` 抽样**再按位置 concat（注意先眼动后心电）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from core.A2_analyze.ecg import EcgProcessor
from core.A2_analyze.eye import EyeProcessor
from core.A2_analyze.load_model import FEATURE_COLUMNS, LoadAssessment

log = logging.getLogger(__name__)

ALIGN_RANDOM_STATE = 42          # 老代码 get_input 里的 random_state


def _section(config: Optional[Dict[str, Any]], name: str) -> Dict[str, Any]:
    if not config:
        return {}
    section = config.get(name)
    return dict(section) if isinstance(section, dict) else {}


def build_features(db_path: str, start: Any = None, end: Any = None,
                   config: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """会话库 -> 10 维特征表（pupil, blink_rate, bpm, rmssd, vlf, lf, hf, lf/hf, sd1, sd2/sd1, timestamp）。

    config 就是 default.yaml 里的 `analyze` 段（可缺省，缺省用各模块默认参数）。
    """
    ecg_features = EcgProcessor(_section(config, 'ecg')).analyze(db_path, start, end)
    eye_features = EyeProcessor(_section(config, 'eye')).analyze(db_path, start, end)

    if ecg_features.empty or eye_features.empty:
        raise RuntimeError(
            f'特征表为空（心电 {len(ecg_features)} 行 / 眼动 {len(eye_features)} 行）：'
            f'时间窗可能比数据还长，检查 analyze.ecg.window_size / analyze.eye.window_size')

    min_len = min(len(ecg_features), len(eye_features))
    if len(ecg_features) > min_len:
        ecg_features = ecg_features.sample(n=min_len, random_state=ALIGN_RANDOM_STATE).sort_index()
    if len(eye_features) > min_len:
        eye_features = eye_features.sample(n=min_len, random_state=ALIGN_RANDOM_STATE).sort_index()

    features = pd.concat([eye_features.reset_index(drop=True),
                          ecg_features.reset_index(drop=True)], axis=1)
    log.info('[analyze] 特征表 %d 行（心电 %d / 眼动 %d 对齐后）',
             len(features), len(ecg_features), len(eye_features))
    return features


def assess(features: pd.DataFrame, models: Optional[Any] = None,
           models_dir: Optional[str] = None,
           config: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """10 维特征表 -> 负荷等级预测表（每个模型一列 + time）。"""
    if models is None:
        models = config.get('models') if config else None
    if models_dir is None:
        models_dir = config.get('models_dir') if config else None
    feature_columns = (config or {}).get('feature_columns') or FEATURE_COLUMNS
    timestamp_column = (config or {}).get('timestamp_column') or 'timestamp'
    return LoadAssessment(models_dir=models_dir, models=models,
                          feature_columns=feature_columns,
                          timestamp_column=timestamp_column).predict(features)


def analyze(db_path: str, start: Any = None, end: Any = None,
            models: Optional[Any] = None, models_dir: Optional[str] = None,
            config: Optional[Dict[str, Any]] = None,
            return_features: bool = False) -> Any:
    """一条龙：会话库 -> 负荷等级。return_features=True 时返回 (特征表, 预测表)。"""
    features = build_features(db_path, start, end, config)
    results = assess(features, models=models, models_dir=models_dir, config=config)
    if return_features:
        return features, results
    return results
