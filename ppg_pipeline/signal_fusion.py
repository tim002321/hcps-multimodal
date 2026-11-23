# -*- coding: utf-8 -*-
"""
信号级融合模块。

步骤：
  1) 逐通道脉搏峰检测(自适应极性)，输出峰值位置、峰间距(PPI)序列；
  2) 逐通道波形质量(SQI)评估(峰间距规则性、峰幅一致性、检出覆盖、运动污染)；
  3) 同步(各通道同一等间隔时间轴，另做互相关延迟校核)、滤波、幅值标准化(z-score)；
  4) 按信号质量权重加权平均得到信号级融合结果。
"""
import numpy as np
from scipy import signal
import config as cfg


# ------------------------------------------------------------------ 峰检测
def _select_polarity(x, fs):
    """尝试正/负两种极性，选择峰间距更规则(PPI CV 更小)的极性。"""
    distance = int(fs * cfg.PEAK["ppi_min"])        # 最小峰间距(样本)
    prom_base = np.std(x) * 0.3
    best = None
    for sign in (1.0, -1.0):
        s = sign * x
        pk, props = signal.find_peaks(s, distance=distance,
                                      prominence=prom_base, height=0.0)
        if len(pk) < 3:
            continue
        ppi = np.diff(pk) / fs
        cv = np.std(ppi) / np.mean(ppi)
        if best is None or cv < best["cv"]:
            best = dict(sign=sign, peaks=pk, ppi=ppi, cv=cv, props=props)
    return best


def detect_peaks(x, fs):
    """返回 dict：peaks(样本索引), ppi(秒), polarity, cv。"""
    res = _select_polarity(x, fs)
    if res is None:
        return dict(peaks=np.array([]), ppi=np.array([]), polarity=1.0,
                    cv=float("nan"))
    return dict(peaks=res["peaks"], ppi=res["ppi"],
                polarity=res["sign"], cv=res["cv"])


# ------------------------------------------------------------------ 波形质量
def _clean_ppi(ppi, fs):
    """剔除生理上不可能的 PPI(0.3–1.5 s)，再做中位数±3MAD 修剪。"""
    p = np.asarray(ppi, dtype=np.float64)
    p = p[(p >= cfg.PEAK["ppi_min"]) & (p <= cfg.PEAK["ppi_max"])]
    if len(p) < 5:
        return p
    med = np.median(p)
    mad = np.median(np.abs(p - med))
    if mad > 0:
        p = p[np.abs(p - med) <= 3 * mad]
    return p


def waveform_quality(x, peaks, ppi, motion_mask, fs):
    """
    计算通道波形质量 SQI ∈ [0,1]。四项分指标：
      q_reg  : 峰间距规则性(1-CV)，CV 越小越规则
      q_amp  : 峰幅值一致性(1 - 峰幅 CV)
      q_cov  : 检出覆盖(实际峰数 / 期望峰数，期望由中值 PPI 推算)
      q_mot  : 运动污染(运动段能量占比的补)
    """
    ppi_c = _clean_ppi(ppi, fs)
    if len(ppi_c) < 5 or len(peaks) < 5:
        return dict(sqi=0.0, q_reg=0.0, q_amp=0.0, q_cov=0.0, q_mot=0.0,
                    n_peaks=int(len(peaks)), mean_ppi=float("nan"))
    cv = np.std(ppi_c) / np.mean(ppi_c)
    q_reg = float(np.clip(1.0 - cv, 0.0, 1.0))

    # 峰幅一致性(用峰位置的局部高度)
    heights = x[peaks]
    amp_cv = np.std(heights) / (np.mean(heights) + 1e-12)
    q_amp = float(np.clip(1.0 - min(amp_cv, 1.5), 0.0, 1.0))

    # 检出覆盖
    med_ppi = float(np.median(ppi_c))
    expected = len(x) / (med_ppi * fs) if med_ppi > 0 else len(peaks)
    q_cov = float(np.clip(len(peaks) / expected, 0.0, 1.0))

    # 运动污染(运动段信号能量占比的补)
    if motion_mask is None or not motion_mask.any():
        q_mot = 1.0
    else:
        e_all = np.mean(x ** 2) + 1e-12
        e_mot = np.mean(x[motion_mask] ** 2) + 1e-12
        q_mot = float(np.clip(1.0 - (e_mot / e_all) * 0.5, 0.0, 1.0))

    sqi = float(np.clip(0.35 * q_reg + 0.25 * q_amp + 0.2 * q_cov + 0.2 * q_mot,
                        0.0, 1.0))
    return dict(sqi=sqi, q_reg=q_reg, q_amp=q_amp, q_cov=q_cov, q_mot=q_mot,
                n_peaks=int(len(peaks)), mean_ppi=float(np.mean(ppi_c)))


# ------------------------------------------------------------------ 同步/标准化/融合
def cross_corr_delay(x, ref, fs, max_lag_s=0.05):
    """以 ref 为参考，估计 x 的相对延迟(秒)，用于同步校核。"""
    max_lag = int(max_lag_s * fs)
    c = signal.correlate(x - x.mean(), ref - ref.mean(), mode="full")
    lag = c.argmax() - (len(x) - 1)
    if abs(lag) > max_lag:
        lag = 0
    return lag / fs


def normalize_zscore(x):
    """幅值标准化：z-score。"""
    s = np.std(x)
    if s < 1e-12:
        return np.zeros_like(x, dtype=np.float64)
    return (x - np.mean(x)) / s


def weighted_fusion(channels, weights):
    """按权重对通道信号加权平均。channels: dict; weights: dict。"""
    keys = list(channels.keys())
    W = np.array([weights[k] for k in keys])
    if W.sum() <= 0:
        W = np.ones(len(keys))
    W = W / W.sum()
    fused = sum(W[i] * channels[k] for i, k in enumerate(keys))
    return fused, W


# ------------------------------------------------------------------ 主入口
def signal_level_fusion(preprocessed, fs_ppg, fs_acc):
    """
    preprocessed: dict[channel] -> preprocess.preprocess_channel 的返回
    返回 (result, per_channel)。
    """
    channels = sorted(preprocessed.keys())
    per = {}
    ref_sig = None
    for ch in channels:
        d = preprocessed[ch]
        x = d["clean"]
        pk = detect_peaks(x, fs_ppg)
        sq = waveform_quality(x, pk["peaks"], pk["ppi"], d["motion_mask"], fs_ppg)
        z = normalize_zscore(x)
        if ref_sig is None:                  # 首个通道：无参考，延迟置 0
            delay = 0.0
            ref_sig = z.copy()
        else:                                # 其余通道：相对首个通道估计延迟
            delay = cross_corr_delay(z, ref_sig, fs_ppg)
        per[ch] = dict(peaks_idx=pk["peaks"], peaks_t=pk["peaks"] / fs_ppg,
                       ppi=pk["ppi"], polarity=pk["polarity"],
                       hr=60.0 / sq["mean_ppi"] if np.isfinite(sq["mean_ppi"]) and sq["mean_ppi"] > 0 else float("nan"),
                       delay_s=delay, z=z, **sq)

    # 融合适用性判断
    thr = 0.5
    fusion_ok = {ch: per[ch]["sqi"] >= thr for ch in channels}

    # 同步后滤波(所有通道已带通预处理，此处仅统一 z-score 平滑，可选)
    channels_z = {ch: per[ch]["z"] for ch in channels}
    weights = {ch: per[ch]["sqi"] for ch in channels}
    fused, W = weighted_fusion(channels_z, weights)
    # 融合信号再轻平滑去除拼接毛刺(5Hz 低通)
    b, a = signal.butter(2, 5.0, btype="low", fs=fs_ppg)
    fused_smooth = signal.filtfilt(b, a, fused)

    result = dict(
        fused_raw=fused,
        fused_smooth=fused_smooth,
        weights={ch: float(weights[ch]) for ch in channels},
        norm_weights={ch: float(w) for ch, w in zip(channels, W)},
        fusion_ok=fusion_ok,
        threshold=thr,
        hr_by_channel={ch: per[ch]["hr"] for ch in channels},
    )
    return result, per
