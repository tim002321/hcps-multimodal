# -*- coding: utf-8 -*-
"""特征提取层：从已经入库的原始序列算出可解释的生理指标。

设计约束：
  * **只用 numpy**。主程序 `requirements.txt` 里没有 scipy / biosppy，
    所以这里不复制 `load_analyze/ecg_process.py` 那套（它依赖 biosppy + hrvanalysis），
    而是给一份自包含的轻量实现，保证 `pip install -r requirements.txt` 就能跑；
  * **算不出来就返回 None + 原因**，不要瞎填数字。界面据此显示 `n/a`
    （电极没贴时设备照样推 ±1 噪声，硬算会报出 190+ 的假心率）。

对外接口：
    extract_hrv(ecg, fs)          -> (dict | None, 说明)
    extract_pulse(ppg, fs)        -> (dict | None, 说明)
    analyze_packets(packets, t0, t1) -> 分析结果字典
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# 通用信号工具
# ---------------------------------------------------------------------------

def _moving_average(x: np.ndarray, size: int) -> np.ndarray:
    """滑动均值。两端用边缘值延拓，避免 `convolve(mode='same')` 零填充造成的边沿尖峰
    （零填充会让首尾几百个点的"基线"掉到 0，去直流后变成巨大的假幅度，
    后面按分位数取阈值时整段信号就废了）。"""
    size = max(1, int(size))
    if size == 1:
        return x.copy()
    pad = size // 2
    padded = np.pad(x, (pad, pad), mode='edge')
    kernel = np.ones(size) / size
    return np.convolve(padded, kernel, mode='same')[pad:pad + x.size]


def bandpass_emph(x: np.ndarray, fs: float, fast_sec: float, slow_sec: float) -> np.ndarray:
    """差分移动平均近似带通：压掉基线漂移和高频噪声，突出 QRS / 脉搏波主频。

    与前端的 `estimate_heart_rate` 用的是同一套思路，参数可按信号调整。
    """
    x = np.asarray(x, dtype=float)
    if x.size < 4:
        return x
    fast = max(2, int(fast_sec * fs))
    slow = max(fast + 2, int(slow_sec * fs))
    return _moving_average(x, fast) - _moving_average(x, slow)


def find_peaks(y: np.ndarray, fs: float, min_interval_sec: float,
               rel_threshold: float = 0.45) -> np.ndarray:
    """一阶导式找峰：局部极大 + 最小间隔（不应期）。

    阈值取 |y| 的 99 分位的 `rel_threshold` 倍，自适应不同幅度的信号；
    幅值太小（< 1e-9）直接返回空，避免把噪声当峰。
    """
    y = np.asarray(y, dtype=float)
    if y.size < 3:
        return np.array([], dtype=int)
    amplitude = float(np.percentile(np.abs(y), 99))
    if not np.isfinite(amplitude) or amplitude < 1e-9:
        return np.array([], dtype=int)

    threshold = rel_threshold * amplitude
    refractory = max(1, int(min_interval_sec * fs))
    peaks: List[int] = []
    last = -refractory
    for index in range(1, y.size - 1):
        if y[index] > threshold and y[index] >= y[index - 1] and y[index] >= y[index + 1]:
            if index - last > refractory:
                peaks.append(index)
                last = index
    return np.array(peaks, dtype=int)


def signal_quality(x: np.ndarray, min_span: float = 50.0, saturate_ratio: float = 32000.0) -> str:
    """粗判信号可用性：ok / weak / bad / wait。"""
    arr = np.asarray(x, dtype=float)
    if arr.size < 100:
        return 'wait'
    if np.mean(np.abs(arr) > saturate_ratio) > 0.02:
        return 'bad'
    if np.percentile(arr, 99.5) - np.percentile(arr, 0.5) < min_span:
        return 'weak'
    return 'ok'


def estimate_fs(timestamps: Sequence[float], count: int,
                fallback: float = 250.0) -> float:
    """按「样本数 / 时间跨度」估采样率，估不出来用标称值。"""
    if count <= 1 or len(timestamps) < 2:
        return fallback
    span = float(timestamps[-1]) - float(timestamps[0])
    if span <= 0:
        return fallback
    fs = count / span
    if not (10.0 <= fs <= 5000.0):
        return fallback
    return float(fs)


# ---------------------------------------------------------------------------
# 心率变异性（HRV）
# ---------------------------------------------------------------------------

def _rr_from_ecg(ecg: np.ndarray, fs: float) -> np.ndarray:
    """ECG → RR 间期（毫秒），已剔除明显异常值。"""
    filtered = bandpass_emph(ecg, fs, fast_sec=0.012, slow_sec=0.08)
    peaks = find_peaks(filtered, fs, min_interval_sec=0.25)
    if peaks.size < 3:
        return np.array([])
    rr = np.diff(peaks) / fs * 1000.0
    # 剔除 300~2000ms 之外的间期（与 hrv-analysis 的推荐区间一致）
    return rr[(rr >= 300.0) & (rr <= 2000.0)]


def _freq_domain(rr_ms: np.ndarray, fs_interp: float = 4.0):
    """把 RR 间期序列插值成等间隔序列后做 FFT，取 VLF/LF/HF 功率。"""
    if rr_ms.size < 8:
        return None
    times = np.cumsum(rr_ms) / 1000.0
    times -= times[0]
    if times[-1] <= 0:
        return None
    grid = np.arange(0.0, times[-1], 1.0 / fs_interp)
    if grid.size < 8:
        return None
    interp = np.interp(grid, times, rr_ms)
    interp = interp - interp.mean()

    spectrum = np.abs(np.fft.rfft(interp * np.hanning(interp.size))) ** 2
    freqs = np.fft.rfftfreq(interp.size, d=1.0 / fs_interp)

    def power(low: float, high: float) -> float:
        mask = (freqs >= low) & (freqs < high)
        if not mask.any():
            return 0.0
        df = freqs[1] - freqs[0] if freqs.size > 1 else 1.0
        return float(np.sum(spectrum[mask]) * df)

    vlf = power(0.0033, 0.04)
    lf = power(0.04, 0.15)
    hf = power(0.15, 0.4)
    total = lf + hf
    return {
        'vlf': vlf,
        'lf': lf,
        'hf': hf,
        'lf_hf': (lf / hf) if hf > 1e-12 else None,
        'lf_nu': (lf / total * 100.0) if total > 1e-12 else None,
    }


def extract_hrv(ecg: Sequence[float], fs: float) -> Tuple[Optional[Dict[str, Any]], str]:
    """心电 → HRV 指标。返回 (特征字典, 说明)；算不出来时字典为 None。"""
    arr = np.asarray(ecg, dtype=float)
    if arr.size < fs * 5:
        return None, f'心电数据太短（{arr.size / max(fs, 1):.1f} 秒，至少 5 秒）'

    quality = signal_quality(arr)
    if quality == 'bad':
        return None, '心电饱和（电极可能脱落）'
    if quality == 'weak':
        return None, '心电信号过弱（n/a）'
    if quality == 'wait':
        return None, '心电数据不足（n/a）'

    rr = _rr_from_ecg(arr, fs)
    if rr.size < 4:
        return None, '未检出足够的 R 峰（n/a）'
    if rr.std() / max(rr.mean(), 1e-6) > 0.5:
        return None, 'RR 间期过于不齐，不像窦性心律（n/a）'

    # 覆盖率：被采纳的 RR 间期总时长 / 整段时长。
    # 电极半脱落时只会在零星片段检出 R 峰，这时中位数算出来的 bpm 会离谱
    # （实测出现过 291 秒里只覆盖 73 秒、却报 178bpm 的情况），必须拒报。
    duration_ms = arr.size / max(fs, 1e-6) * 1000.0
    coverage = float(rr.sum()) / duration_ms if duration_ms > 0 else 0.0
    if coverage < 0.5:
        return None, 'R 峰检出覆盖率仅 {:.0f}%，信号不连续（n/a）'.format(coverage * 100)

    diff = np.diff(rr)
    bpm = 60000.0 / float(np.median(rr))
    if not (30.0 <= bpm <= 220.0):
        # 落在人类心率之外，说明检出的多半是噪声峰而不是 QRS
        return None, f'心率 {bpm:.0f} bpm 不合理，判定为噪声（n/a）'
    features: Dict[str, Any] = {
        '心跳数': int(rr.size + 1),
        'bpm': round(bpm, 1),
        'sdnn': round(float(np.std(rr, ddof=1)), 2),
        'rmssd': round(float(np.sqrt(np.mean(diff ** 2))), 2) if diff.size else None,
        'pnn50': round(float(np.mean(np.abs(diff) > 50) * 100.0), 1) if diff.size else None,
        'sd1': round(float(np.sqrt(0.5) * np.std(diff, ddof=1)), 2) if diff.size > 1 else None,
        'sd2': round(float(np.sqrt(max(2 * np.var(rr, ddof=1) - 0.5 * np.var(diff, ddof=1), 0.0))), 2)
        if diff.size > 1 else None,
    }

    freq = _freq_domain(rr)
    if freq:
        features.update({
            'vlf': round(freq['vlf'], 1),
            'lf': round(freq['lf'], 1),
            'hf': round(freq['hf'], 1),
            'lf/hf': round(freq['lf_hf'], 2) if freq['lf_hf'] is not None else None,
        })
    return features, 'ok'


# ---------------------------------------------------------------------------
# PPG 脉搏
# ---------------------------------------------------------------------------

def extract_pulse(ppg: Sequence[float], fs: float) -> Tuple[Optional[Dict[str, Any]], str]:
    """单通道 PPG → 脉搏率 / 脉间期 / 幅度。输入应是**未去直流**的原始值。"""
    arr = np.asarray(ppg, dtype=float)
    if arr.size < fs * 5:
        return None, f'PPG 数据太短（{arr.size / max(fs, 1):.1f} 秒，至少 5 秒）'

    # 24bit 原始值的直流分量有上百万，先做交流耦合再检测
    ac = arr - _moving_average(arr, max(3, int(fs * 0.8)))
    span = float(np.percentile(ac, 99) - np.percentile(ac, 1))
    if span < 1e-6:
        return None, 'PPG 无脉搏信号（未贴合 / 无接触，n/a）'

    filtered = bandpass_emph(ac, fs, fast_sec=0.05, slow_sec=0.35)
    peaks = find_peaks(filtered, fs, min_interval_sec=0.33, rel_threshold=0.35)
    if peaks.size < 3:
        return None, '未检出脉搏波（n/a）'

    ppi = np.diff(peaks) / fs * 1000.0
    ppi = ppi[(ppi >= 300.0) & (ppi <= 2000.0)]
    if ppi.size < 2:
        return None, '脉间期数量不足（n/a）'

    amplitude = float(np.mean(np.abs(filtered[peaks])))
    return {
        '脉搏波个数': int(peaks.size),
        '脉率': round(60000.0 / float(np.median(ppi)), 1),
        '脉间期均值': round(float(ppi.mean()), 1),
        '脉间期标准差': round(float(ppi.std(ddof=1)), 1),
        '脉搏幅度': round(amplitude, 1),
    }, 'ok'


# ---------------------------------------------------------------------------
# 面向「已入库片段」的整段分析
# ---------------------------------------------------------------------------

def _collect(packets, kind: str, key_path: Sequence[str], t0: float, t1: float):
    """从回放包里挑出 [t0, t1] 内某类信号的数值序列与时间戳。"""
    values: List[float] = []
    stamps: List[float] = []
    for ts, pkt_kind, decoded in packets:
        if pkt_kind != kind or ts < t0 or ts > t1:
            continue
        node: Any = decoded
        for step in key_path:
            if not isinstance(node, dict) or step not in node:
                node = None
                break
            node = node[step]
        if node is None:
            continue
        if isinstance(node, (list, tuple)):
            values.extend(float(v) for v in node)
        else:
            values.append(float(node))
        stamps.append(float(ts))
    return values, stamps


def _collect_ppg(packets, start: float, end: float,
                 channel: str = 'PPG1') -> Tuple[List[float], List[float]]:
    """手环 PPG 包是 `{'PPG': [{PPG1..PPG4}, …]}`，按通道展开成单条序列。"""
    values: List[float] = []
    stamps: List[float] = []
    for ts, kind, decoded in packets:
        if kind != 'bracelet' or ts < start or ts > end:
            continue
        for point in (decoded.get('PPG') or []):
            if channel in point:
                values.append(float(point[channel]))
                stamps.append(float(ts))
    return values, stamps


def analyze_packets(packets, start: Optional[float] = None,
                    end: Optional[float] = None) -> Dict[str, Any]:
    """对一段会话数据做特征提取。

    参数 `packets` 是 `ui.replay.load_packets()` 的返回值；`start/end` 为 epoch 秒，
    不传则用全部数据。返回结构供界面直接渲染：

        {
          '区间': (start, end, 时长秒),
          '心电': {'状态': 'ok'|'n/a', '说明': str, '特征': {...}},
          '手环PPG': {...},
          '眼动':   {'状态': 'n/a', '说明': '眼动仪未接入'},
          '表情':   {'状态': 'n/a', '说明': 'YOLO 模型未接入'},
        }
    """
    if not packets:
        return {'区间': (0, 0, 0), '心电': {'状态': 'n/a', '说明': '没有数据'},
                '手环PPG': {'状态': 'n/a', '说明': '没有数据'},
                '眼动': {'状态': 'n/a', '说明': '眼动仪未接入'},
                '表情': {'状态': 'n/a', '说明': '表情模型未接入'}}

    first = packets[0][0]
    last = packets[-1][0]
    start = first if start is None else max(first, start)
    end = last if end is None else min(last, end)
    if end <= start:
        return {'区间': (start, end, 0), '心电': {'状态': 'n/a', '说明': '时间区间为空'},
                '手环PPG': {'状态': 'n/a', '说明': '时间区间为空'},
                '眼动': {'状态': 'n/a', '说明': '眼动仪未接入'},
                '表情': {'状态': 'n/a', '说明': '表情模型未接入'}}

    result: Dict[str, Any] = {'区间': (start, end, end - start)}

    ecg, ecg_ts = _collect(packets, 'ecg', ('ECG', 'ECG'), start, end)
    ecg_fs = estimate_fs(ecg_ts, len(ecg), fallback=250.0)
    hrv, note = extract_hrv(ecg, ecg_fs) if ecg else (None, '区间内没有心电数据')
    result['心电'] = {'状态': 'ok' if hrv else 'n/a', '说明': note,
                      '特征': hrv or {}, '采样率': round(ecg_fs, 1), '采样点': len(ecg)}

    ppg, ppg_ts = _collect_ppg(packets, start, end)
    ppg_fs = estimate_fs(ppg_ts, len(ppg), fallback=250.0)
    pulse, pulse_note = extract_pulse(ppg, ppg_fs) if ppg else (None, '区间内没有 PPG 数据')
    result['手环PPG'] = {'状态': 'ok' if pulse else 'n/a', '说明': pulse_note,
                         '特征': pulse or {}, '采样率': round(ppg_fs, 1), '采样点': len(ppg)}

    result['眼动'] = {'状态': 'n/a', '说明': '眼动仪未接入（预留）', '特征': {}}
    result['表情'] = {'状态': 'n/a', '说明': '表情模型未接入（预留 YOLO）', '特征': {}}
    return result
