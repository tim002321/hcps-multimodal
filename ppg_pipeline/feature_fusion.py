# -*- coding: utf-8 -*-
"""
特征级融合模块。

各通道分别提取：
  - 心率 HR(bpm)
  - PRV 时域: Mean PPI、SDNN、RMSSD、pNN50
  - PRV 频域: LF、HF、LF/HF、TP、LFnu、HFnu(PPI 插值重采样后 Welch 谱积分)
  - PRV 非线性: SD1、SD2、SD1/SD2、样本熵 SampEn

融合方式(两种均实现)：
  1) 拼接(concatenate)：所有通道特征拼接成一个向量；
  2) 注意力融合：以各通道波形质量 SQI 经 softmax 为注意力权重，对特征向量
     逐维加权求和得到融合特征；同时输出 HR 的质量加权融合值。
"""
import numpy as np
from scipy import signal
from scipy.spatial.distance import pdist
import config as cfg

# np.trapezoid 是 numpy 2.0 才有的名字；1.x 里叫 np.trapz。这里做一次兼容，
# 免得本机 numpy 1.26 直接 AttributeError。
_trapz = getattr(np, "trapezoid", None) or np.trapz


def _clean_ppi(ppi):
    p = np.asarray(ppi, dtype=np.float64)
    p = p[(p >= cfg.PEAK["ppi_min"]) & (p <= cfg.PEAK["ppi_max"])]
    if len(p) < 5:
        return p
    med = np.median(p); mad = np.median(np.abs(p - med))
    if mad > 0:
        p = p[np.abs(p - med) <= 3 * mad]
    return p


def _freq_features(ppi, fs_resample=None):
    """PPI 插值重采样 + Welch 谱，返回频域指标(功率单位统一为 ms^2)。"""
    res = cfg.PRV_FREQ["resample_hz"]
    lf = cfg.PRV_FREQ["lf"]; hf = cfg.PRV_FREQ["hf"]
    t = np.cumsum(np.r_[0.0, ppi])                       # 峰事件时刻(秒)
    tq = np.arange(0.0, t[-1], 1.0 / res)
    x = np.interp(tq, t[:-1], ppi)                        # 均匀重采样 PPI(秒)
    x = signal.detrend(x)
    f, Pxx = signal.welch(x, fs=res, nperseg=min(256, len(x)), detrend="constant")
    def _band(b):
        m = (f >= b[0]) & (f <= b[1])
        return float(_trapz(Pxx[m], f[m])) if m.any() else 0.0
    LF = _band(lf) * 1e6; HF = _band(hf) * 1e6           # s^2 -> ms^2
    TP = float(_trapz(Pxx, f)) * 1e6
    lfhf = LF / HF if HF > 1e-12 else float("inf")
    lfnu = LF / (LF + HF) * 100 if (LF + HF) > 1e-12 else float("nan")
    hfnu = HF / (LF + HF) * 100 if (LF + HF) > 1e-12 else float("nan")
    return dict(LF_ms2=LF, HF_ms2=HF, LFHF=lfhf, TP_ms2=TP,
                LFnu=lfnu, HFnu=hfnu)


def _sample_entropy(x, m=2, r=None):
    """样本熵(SampEn)。"""
    x = np.asarray(x, dtype=np.float64)
    N = len(x)
    if N < 20:
        return float("nan")
    r = 0.2 * np.std(x) if r is None else r
    if r <= 0:
        return float("nan")
    def _phi(mdim):
        n = N - mdim
        tm = np.stack([x[i:i + mdim] for i in range(n)])
        dm = pdist(tm, metric="chebyshev")
        return float(np.sum(dm < r))
    A = _phi(m + 1); B = _phi(m)
    if B <= 0 or A <= 0:
        return float("nan")
    return -float(np.log(A / B))


def _nonlinear_features(ppi):
    d = np.diff(ppi)
    sd1 = np.std(d) / np.sqrt(2.0) * 1000.0              # 秒 -> 毫秒
    sd2 = np.sqrt(2.0 * np.var(ppi) - 0.5 * np.var(d)) * 1000.0
    se = _sample_entropy(ppi)
    return dict(SD1_ms=sd1, SD2_ms=sd2,
                SD1SD2=(sd1 / sd2 if sd2 > 1e-12 else float("nan")),
                SampEn=se)


def extract_features(peaks_idx, fs):
    """
    由峰索引提取一个通道的全部 PRV 特征。
    返回 dict(含 HR 与各 PRV 指标)。
    """
    ppi = np.diff(peaks_idx) / fs
    ppi_c = _clean_ppi(ppi)
    if len(ppi_c) < 5:
        return dict(HR_bpm=float("nan"), Mean_PPI_ms=float("nan"),
                    SDNN_ms=float("nan"), RMSSD_ms=float("nan"),
                    pNN50=float("nan"), **{k: float("nan") for k in
                    ["LF_ms2", "HF_ms2", "LFHF", "TP_ms2", "LFnu", "HFnu"]},
                    **{k: float("nan") for k in
                    ["SD1_ms", "SD2_ms", "SD1SD2", "SampEn"]})
    ppi_ms = ppi_c * 1000.0
    mean_ppi = float(np.mean(ppi_ms))
    sdnn = float(np.std(ppi_ms, ddof=1))
    diffs = np.abs(np.diff(ppi_ms))
    rmssd = float(np.sqrt(np.mean(diffs ** 2)))
    pnn50 = float(np.mean(diffs > 50.0) * 100.0)
    hr = 60.0 / (mean_ppi / 1000.0)
    feat = dict(HR_bpm=hr, Mean_PPI_ms=mean_ppi, SDNN_ms=sdnn,
                RMSSD_ms=rmssd, pNN50=pnn50)
    feat.update(_freq_features(ppi_c))
    feat.update(_nonlinear_features(ppi_c))
    return feat


# ------------------------------------------------------------------ 融合
_FEATURE_KEYS = ["HR_bpm", "Mean_PPI_ms", "SDNN_ms", "RMSSD_ms", "pNN50",
                 "LF_ms2", "HF_ms2", "LFHF", "TP_ms2", "LFnu", "HFnu",
                 "SD1_ms", "SD2_ms", "SD1SD2", "SampEn"]


def attention_weights(sqi_scores, temperature=None):
    """softmax 注意力权重。"""
    t = temperature or cfg.ATTENTION["temperature"]
    s = np.asarray([sqi_scores[k] for k in sorted(sqi_scores)])
    e = np.exp((s - s.max()) / t)
    return {k: float(w) for k, w in zip(sorted(sqi_scores), e / e.sum())}


def concatenate_features(features):
    """拼接：各通道特征向量顺次拼接。"""
    keys = sorted(features)
    vec = np.concatenate([np.array([features[k][f] if np.isfinite(features[k][f])
                                     else 0.0 for f in _FEATURE_KEYS])
                          for k in keys])
    return dict(concat_vector=vec,
                feature_names=[f"_{k}_{kk}" for k in keys for kk in _FEATURE_KEYS])


def attention_fuse(features, sqi_scores):
    """
    注意力融合：a_i=softmax(SQI_i)，融合特征 = Σ a_i * feature_i(逐维加权)。
    返回 (feature_dict, attention_dict)。
    """
    keys = sorted(features)
    att = attention_weights(sqi_scores)
    vec = np.zeros(len(_FEATURE_KEYS))
    for k in keys:
        row = np.array([features[k][f] if np.isfinite(features[k][f]) else 0.0
                        for f in _FEATURE_KEYS])
        vec += att[k] * row
    # HR 质量加权融合(仅 HR 的软注意力)
    hr_fused = sum(att[k] * (features[k]["HR_bpm"] if np.isfinite(features[k]["HR_bpm"]) else 0.0)
                   for k in keys)
    out = dict(zip(_FEATURE_KEYS, [float(v) for v in vec]))
    out["HR_bpm"] = float(hr_fused) if np.isfinite(hr_fused) else float("nan")
    return out, att


def feature_level_fusion(features_by_channel, sqi_scores):
    """主入口。features_by_channel: dict[ch]->extract_features 返回。"""
    concat = concatenate_features(features_by_channel)
    att_feat, att_weights = attention_fuse(features_by_channel, sqi_scores)
    return dict(
        per_channel=features_by_channel,
        concat_vector=concat["concat_vector"],
        concat_feature_names=concat["feature_names"],
        attention_fused=att_feat,
        attention_weights=att_weights,
    )
