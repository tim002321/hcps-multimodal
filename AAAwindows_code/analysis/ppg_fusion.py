# -*- coding: utf-8 -*-
"""四通道 PPG 融合分析（复用工作区里的 `ppg_pipeline`）。

为什么单独一层：
  * `ppg_pipeline` 是独立脚本工程（平铺 import：`import config` / `import preprocess`），
    直接 import 会把 `config` 这种通用名字塞进 sys.path。统一在这里做一次受控加载，
    业务代码只依赖本模块的 `analyze()`。
  * 该管线依赖 scipy。主程序的 `requirements.txt` 不带 scipy，所以这里必须
    **缺 scipy 时优雅退化**，让上层的单通道轻量算法继续工作。

对外：
    available()                 -> (bool, 说明)
    analyze(db_path, start, end)-> (features: dict | None, note: str, fs: float | None)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from core.paths import DATA_DIR, resolve_resource_dir

# 源码运行时管线在工作区根目录（AAAwindows_code 的上一级）；打包后随包放在资源目录里。
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_pipeline_dir() -> str:
    candidates = [
        os.environ.get('HCP_PPG_PIPELINE') or '',
        resolve_resource_dir('ppg_pipeline'),                 # 打包后 / exe 旁边自带的
        os.path.join(os.path.dirname(_APP_DIR), 'ppg_pipeline'),  # 源码运行：工作区根目录
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return candidates[1]


_PIPELINE_DIR = _find_pipeline_dir()

_loaded: Optional[Dict[str, Any]] = None
_load_error = ''


def _ensure_loaded() -> Optional[Dict[str, Any]]:
    """按需加载 ppg_pipeline 的各个模块，失败原因记在 _load_error。"""
    global _loaded, _load_error
    if _loaded is not None:
        return _loaded
    if _load_error:
        return None

    if not os.path.isdir(_PIPELINE_DIR):
        _load_error = '没找到 ppg_pipeline 目录（{}）'.format(_PIPELINE_DIR)
        return None

    try:
        import scipy  # noqa: F401  - 只有 import 成功才谈得上跑管线
    except Exception:                                   # noqa: BLE001
        _load_error = '未安装 scipy（四通道融合需要），已退回单通道算法'
        return None

    # config 在 import 时就会 makedirs(OUTPUT_DIR)，先把输出目录指到工作区里，
    # 免得它在别的盘上创建 D:\projects\... 那种目录。
    # config 在 import 时就会 makedirs(OUTPUT_DIR)，指到程序自己的 data/ 下，
    # 免得它在 exe 旁边或别的盘上乱建 ppg_analysis 目录（界面分析不写任何文件）
    os.environ.setdefault('PPG_OUT', os.path.join(DATA_DIR, '_ppg_cache'))

    if _PIPELINE_DIR not in sys.path:
        sys.path.append(_PIPELINE_DIR)          # 放末尾，避免抢占主程序的同名模块
    try:
        import config as ppg_config
        import data_loader
        import feature_fusion
        import preprocess
        import signal_fusion
    except Exception as exc:                            # noqa: BLE001
        _load_error = '加载 ppg_pipeline 失败：{}'.format(exc)
        return None

    _loaded = dict(config=ppg_config, data_loader=data_loader,
                   feature_fusion=feature_fusion, preprocess=preprocess,
                   signal_fusion=signal_fusion)
    return _loaded


def available() -> Tuple[bool, str]:
    """四通道融合能不能用。"""
    mods = _ensure_loaded()
    if mods is None:
        return False, _load_error or '四通道融合不可用'
    return True, '四通道融合可用（{}）'.format(_PIPELINE_DIR)


def _iso(epoch: float) -> str:
    """epoch 秒 → 库里 time 列的格式（与 time 列字符串可直接比较）。"""
    return datetime.fromtimestamp(float(epoch)).strftime('%Y-%m-%d %H:%M:%S.%f')


def _r(value: Any, digits: int = 1) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):   # NaN / inf
        return None
    return round(number, digits)


def analyze(db_path: str, start: Optional[float] = None,
            end: Optional[float] = None
            ) -> Tuple[Optional[Dict[str, Any]], str, Optional[float]]:
    """在会话库上跑四通道融合，返回 (特征字典, 说明, 采样率)。

    特征字典的键就是数据分析页「特征结果」表里显示的项目名。
    """
    mods = _ensure_loaded()
    if mods is None:
        return None, _load_error or '四通道融合不可用', None
    if not db_path or not os.path.exists(db_path):
        return None, '会话库不存在', None

    cfg = mods['config']
    loader = mods['data_loader']
    prep = mods['preprocess']
    sfun = mods['signal_fusion']
    ffun = mods['feature_fusion']

    t0 = _iso(start) if start else None
    t1 = _iso(end) if end else None

    try:
        # 新版 data_loader 会额外返回时间轴质量报告（采样率按额定值、按包锚点重建
        # 时间轴）；打包里若带的是旧版 ppg_pipeline，没有 with_qc，退回三元组。
        ppg, fs_ppg, _t, qc_ppg = loader.load_ppg(db_path, t0, t1, with_qc=True)
        ax, ay, az, fs_acc, _t2, qc_acc = loader.load_accel(db_path, t0, t1, with_qc=True)
    except TypeError:                                   # 旧版管线
        try:
            ppg, fs_ppg, _t = loader.load_ppg(db_path, t0, t1)
            ax, ay, az, fs_acc, _t2 = loader.load_accel(db_path, t0, t1)
            qc_ppg = qc_acc = None
        except Exception as exc:                        # noqa: BLE001
            return None, '读取手环数据失败：{}'.format(exc), None
    except Exception as exc:                            # noqa: BLE001
        return None, '读取手环数据失败：{}'.format(exc), None

    channels = [ch for ch in sorted(ppg) if len(ppg[ch])]
    if not channels:
        return None, '区间内没有 PPG 数据', None
    n = len(ppg[channels[0]])
    # 「太短」改用墙钟时长判断：采样率固定 250 后，n/fs 只等于样本跨度，
    # 中途断连过的记录会出现样本很多但实际只录了几秒的情况。
    dur = qc_ppg.duration_wall if qc_ppg else n / max(fs_ppg, 1e-6)
    if dur < 10.0:
        return None, 'PPG 只录到 {:.1f} 秒，太短（至少 10 秒）'.format(dur), fs_ppg
    if len(ax) < 10:
        return None, '区间内没有腕部加速度，无法做运动伪影对消', fs_ppg

    # ---- 四通道预处理 ----
    pre = {}
    for ch in channels:
        pre[ch] = prep.preprocess_channel(ppg[ch], ax, ay, az, fs_ppg, fs_acc)

    # ---- 信号级融合 ----
    sig_result, per_ch = sfun.signal_level_fusion(pre, fs_ppg, fs_acc)

    # ---- 特征级融合 ----
    feats = {ch: ffun.extract_features(per_ch[ch]["peaks_idx"], fs_ppg)
             for ch in channels}
    sqis = {ch: per_ch[ch]["sqi"] for ch in channels}
    feat_result = ffun.feature_level_fusion(feats, sqis)
    fused = feat_result["attention_fused"]

    fused_channels = [ch for ch in channels if sig_result["fusion_ok"].get(ch)]
    hr_text = ' / '.join('{} {:.1f}'.format(ch, per_ch[ch]["hr"]) for ch in channels)
    sqi_text = ' / '.join('{} {:.2f}'.format(ch, per_ch[ch]["sqi"]) for ch in channels)

    features: Dict[str, Any] = {
        '脉率(四通道融合)': _r(fused.get("HR_bpm")),
        '平均脉间期(融合)': _r(fused.get("Mean_PPI_ms")),
        'SDNN': _r(fused.get("SDNN_ms")),
        'RMSSD': _r(fused.get("RMSSD_ms")),
        'pNN50': _r(fused.get("pNN50")),
        'LF/HF': _r(fused.get("LFHF"), 2),
        'SD1/SD2': _r(fused.get("SD1SD2"), 2),
        '样本熵': _r(fused.get("SampEn"), 2),
        '参与融合通道': '、'.join(fused_channels) if fused_channels else '无',
        '各通道脉率': hr_text,
        '各通道 SQI': sqi_text,
        '通道权重': ' / '.join(
            '{} {:.2f}'.format(ch, w) for ch, w in
            sorted(feat_result["attention_weights"].items())),
    }

    note = ('四通道 PPG 融合（信号级 SQI 加权 + 特征级注意力）；'
            '区间内 {} 个采样点 / {:.1f} 秒，fs=额定 {:.0f} Hz'.format(
                n, dur, fs_ppg))
    if qc_ppg is not None:
        note += '（实测 {:.1f} Hz，漂移 {:+.2f} s，断点 {} 处）'.format(
            qc_ppg.fs_measured, qc_ppg.drift_s, len(qc_ppg.breaks))
        if qc_ppg.gap_seconds >= 1.0:
            note += '；有 {:.1f} 秒数据缺口，融合结果请结合断点位置解读'.format(
                qc_ppg.gap_seconds)
    return features, note, fs_ppg
