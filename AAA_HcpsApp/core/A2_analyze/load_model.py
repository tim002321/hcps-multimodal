# -*- coding: utf-8 -*-
"""负荷等级评估（A2）：10 维特征 -> SVM / CatBoost / XGBoost 三模型预测。

来源：老工程 load_analyze/load_assessment.py（界面 load_analyze_window.py
`from load_analyze.load_assessment import load_assessment_function`）。
口径逐字照搬：

* 特征列（顺序也一致）：pupil, blink_rate, bpm, hf, lf/hf, lf, vlf, sd2/sd1, rmssd, sd1
* 缺值先 ffill 再 bfill，然后过 `scaler.pkl`（StandardScaler）标准化
* 每个模型各自 predict；有 `label_encoder.pkl` 就做 inverse_transform（老工程没这个文件，
  所以走的是原始标签分支）
* 结果表加一列 time = 输入的时间戳；输入按时间戳排序后再喂模型
* 某个模型报错就跳过它并告警；全挂了才抛异常

相对老代码的改动（只改壳）：

* 模型目录走配置（老代码把 'saved_models/xxx.pkl' 写死在函数里，必须 cd 到老工程根目录才能跑）
* 模型只加载一次并缓存；缺哪个文件在日志里点名
* print 换成 logging
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

import joblib
import pandas as pd

log = logging.getLogger(__name__)

# 老代码 feature_columns 的原样拷贝（顺序就是喂给 scaler 的顺序，别动）
FEATURE_COLUMNS: List[str] = ['pupil', 'blink_rate', 'bpm', 'hf', 'lf/hf',
                              'lf', 'vlf', 'sd2/sd1', 'rmssd', 'sd1']

MODEL_FILES: Dict[str, str] = {
    'SVM': 'svm_model.pkl',
    'CatBoost': 'catboost_model.cbm',
    'XGBoost': 'xgboost_model.pkl',
}
SCALER_FILE = 'scaler.pkl'
LABEL_ENCODER_FILE = 'label_encoder.pkl'          # 老工程没有这个文件，可选

DEFAULT_MODELS: List[str] = ['SVM', 'CatBoost', 'XGBoost']
TIMESTAMP_COLUMN = 'timestamp'
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'

# 新工程自己的模型目录：AAA_HcpsApp/models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MODELS_DIR = os.path.join(PROJECT_ROOT, 'models')


def resolve_models_dir(models_dir: Optional[str] = None) -> str:
    """模型目录：绝对路径原样用，相对路径按工程根解析。"""
    if not models_dir:
        return DEFAULT_MODELS_DIR
    if os.path.isabs(models_dir):
        return models_dir
    return os.path.join(PROJECT_ROOT, models_dir)


class LoadAssessment:
    """负荷评估：加载一次模型，反复预测。"""

    def __init__(self, models_dir: Optional[str] = None, models: Optional[Sequence[str]] = None,
                 feature_columns: Optional[Sequence[str]] = None,
                 timestamp_column: str = TIMESTAMP_COLUMN):
        self.models_dir = resolve_models_dir(models_dir)
        self.models: List[str] = list(models or DEFAULT_MODELS)
        self.feature_columns: List[str] = list(feature_columns or FEATURE_COLUMNS)
        self.timestamp_column = timestamp_column
        self._loaded: Optional[Dict[str, Any]] = None
        self._scaler = None
        self._label_encoder = None

    # ------------------------------------------------------------- 文件
    def model_path(self, name: str) -> str:
        return os.path.join(self.models_dir, MODEL_FILES[name])

    @property
    def scaler_path(self) -> str:
        return os.path.join(self.models_dir, SCALER_FILE)

    def missing_files(self) -> List[str]:
        """当前配置下缺哪些模型文件（都齐了返回空表）。"""
        wanted = [self.model_path(name) for name in self.models] + [self.scaler_path]
        return [path for path in wanted if not os.path.exists(path)]

    def available_models(self) -> List[str]:
        return [name for name in self.models if os.path.exists(self.model_path(name))]

    # ------------------------------------------------------------- 加载
    def load(self) -> 'LoadAssessment':
        """加载模型和标准化器（幂等，加载过就直接返回）。"""
        if self._loaded is not None:
            return self

        missing = self.missing_files()
        if missing:
            raise FileNotFoundError(
                '缺少模型文件：' + '、'.join(missing) +
                f'（模型目录 {self.models_dir}；老工程的模型在 PyCharmMiscProject/saved_models）')

        loaded: Dict[str, Any] = {}
        for name in self.models:
            path = self.model_path(name)
            try:
                if name == 'CatBoost':
                    from catboost import CatBoostClassifier
                    model = CatBoostClassifier()
                    model.load_model(path)
                else:
                    model = joblib.load(path)
                loaded[name] = model
                log.info('[load] %s 模型已加载：%s', name, path)
            except Exception as exc:                     # noqa: BLE001 - 单个模型失败不拖累其它
                log.warning('[load] %s 模型加载失败：%s', name, exc)

        self._scaler = joblib.load(self.scaler_path)

        encoder_path = os.path.join(self.models_dir, LABEL_ENCODER_FILE)
        if os.path.exists(encoder_path):
            self._label_encoder = joblib.load(encoder_path)
        else:
            self._label_encoder = None
            log.info('[load] 没有 %s，按老代码走原始预测结果', LABEL_ENCODER_FILE)

        if not loaded:
            raise RuntimeError(f'模型一个都没加载成功（目录 {self.models_dir}）')
        self._loaded = loaded
        return self

    # ------------------------------------------------------------- 预测
    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """校验输入 + 按时间排序（老代码 validate_input_data）。"""
        if not isinstance(data, pd.DataFrame):
            raise TypeError('输入必须是 pandas DataFrame')
        if data.empty:
            raise ValueError('输入数据为空')
        if self.timestamp_column not in data.columns:
            raise ValueError(f'缺少时间戳列：{self.timestamp_column}')
        missing = [column for column in self.feature_columns if column not in data.columns]
        if missing:
            raise ValueError(f'缺少特征列：{missing}')

        frame = data.copy()
        try:
            if frame[self.timestamp_column].dtype == 'object':
                frame[self.timestamp_column] = pd.to_datetime(frame[self.timestamp_column],
                                                              format=TIMESTAMP_FORMAT)
            else:
                frame[self.timestamp_column] = pd.to_datetime(frame[self.timestamp_column])
        except Exception as exc:                         # noqa: BLE001
            raise ValueError(f'时间戳格式错误：{exc}') from exc
        return frame.sort_values(by=self.timestamp_column).reset_index(drop=True)

    def predict(self, data: pd.DataFrame) -> pd.DataFrame:
        """10 维特征表 -> 每个模型一列的预测表 + time 列。"""
        self.load()
        frame = self.prepare(data)
        log.info('[load] 开始负荷评估：%d 个样本，模型 %s', len(frame), self.models)

        feature_data = frame[self.feature_columns].copy()
        if feature_data.isnull().any().any():
            log.warning('[load] 特征里有缺失值，按老代码 ffill/bfill 补')
            feature_data = feature_data.ffill().bfill()
            still_null = feature_data.isnull().any()
            if still_null.any():
                # 整列都是 NaN（比如等间距心电算出来的 lf/hf = 0/0）补不出来，
                # 老代码也是这个下场：不支持缺失值的模型（SVM）会被跳过。
                log.warning('[load] 这些列补完还是 NaN：%s；不支持缺失值的模型会被跳过',
                            list(still_null[still_null].index))

        try:
            scaled = self._scaler.transform(feature_data)
        except Exception as exc:                         # noqa: BLE001
            raise RuntimeError(f'数据标准化失败：{exc}') from exc

        predictions: Dict[str, Any] = {}
        for name in self.models:
            model = (self._loaded or {}).get(name)
            if model is None:
                log.warning('[load] %s 未加载，跳过', name)
                continue
            try:
                preds = model.predict(scaled)
                if self._label_encoder is not None:
                    preds = self._label_encoder.inverse_transform(preds.astype(int))
                predictions[name] = preds
                log.info('[load] %s 预测完成', name)
            except Exception as exc:                     # noqa: BLE001
                log.warning('[load] %s 预测失败：%s', name, exc)

        if not predictions:
            raise RuntimeError('所有模型都预测失败')

        results = pd.DataFrame(predictions)
        results['time'] = frame[self.timestamp_column].values
        return results

    def save(self, results: pd.DataFrame, output_path: str) -> str:
        """结果落盘（老代码 save_results）。"""
        directory = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(directory, exist_ok=True)
        results.to_csv(output_path, index=False)
        log.info('[load] 结果已保存：%s', output_path)
        return output_path


def load_assessment(data: pd.DataFrame, models: Optional[Sequence[str]] = None,
                    models_dir: Optional[str] = None,
                    feature_columns: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """便捷入口：等价于老代码 `load_assessment_function(data, selected_models)`。"""
    return LoadAssessment(models_dir=models_dir, models=models,
                          feature_columns=feature_columns).predict(data)
