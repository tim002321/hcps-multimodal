# -*- coding: utf-8 -*-
"""
滑动窗口特征提取模块：窗口长度 300 s(5 min)、步长 60 s(1 min)。
对预处理后的各通道 clean 信号逐窗口做脉搏峰检测与 HR/PRV 特征提取，
并给出每窗口的 SQI 加权融合心率与注意力融合特征，输出"特征随时间变化"的行表。
"""
import numpy as np
import signal_fusion as sf
import feature_fusion as ff


def _safe_hr(mean_ppi):
    if mean_ppi is None or not np.isfinite(mean_ppi) or mean_ppi <= 0:
        return float("nan")
    return float(60.0 / mean_ppi)


def sliding_window_features(pre, fs_ppg, win_sec=300.0, step_sec=60.0):
    """
    pre: dict[channel] -> preprocess.preprocess_channel 的返回(已全段预处理)
    返回 (rows, starts, fs)。
      rows: 每窗口一行特征(dict)，含窗口时间、注意力融合特征、各通道 HR/SQI。
    """
    channels = sorted(pre)
    n = len(pre[channels[0]]["clean"])
    win = max(2, int(win_sec * fs_ppg))
    step = max(1, int(step_sec * fs_ppg))
    starts = list(range(0, n - win + 1, step))      # 仅完整窗口
    rows = []

    for si, s in enumerate(starts):
        i0, i1 = s, s + win
        seg = {ch: pre[ch]["clean"][i0:i1] for ch in channels}
        mm = {ch: pre[ch]["motion_mask"][i0:i1] for ch in channels}

        # 逐通道峰检测 + 波形质量
        per = {}
        for ch in channels:
            pk = sf.detect_peaks(seg[ch], fs_ppg)
            sq = sf.waveform_quality(seg[ch], pk["peaks"], pk["ppi"],
                                     mm[ch], fs_ppg)
            per[ch] = dict(pk=pk, sq=sq)

        # 逐通道特征
        feats = {ch: ff.extract_features(per[ch]["pk"]["peaks"], fs_ppg)
                 for ch in channels}

        # SQI 加权融合心率
        sqis = {ch: per[ch]["sq"]["sqi"] for ch in channels}
        sw = np.array([sqis[c] for c in channels])
        sw = sw / sw.sum() if sw.sum() > 0 else np.ones(len(channels)) / len(channels)
        hr_i = {ch: _safe_hr(per[ch]["sq"].get("mean_ppi")) for ch in channels}
        hr_fused = sum(sw[i] * (hr_i[c] if np.isfinite(hr_i[c]) else 0.0)
                       for i, c in enumerate(channels))

        # 注意力融合特征
        att_feat, _ = ff.attention_fuse(feats, sqis)

        row = dict(window_id=si, t_start_s=round(i0 / fs_ppg, 2),
                   t_end_s=round(i1 / fs_ppg, 2))
        for k in ["HR_bpm", "Mean_PPI_ms", "SDNN_ms", "RMSSD_ms", "pNN50",
                  "LFHF", "LFnu", "HFnu", "SD1SD2", "SampEn"]:
            row[k] = att_feat.get(k, float("nan"))
        row["HR_fused_sqi"] = hr_fused if np.isfinite(hr_fused) else float("nan")
        for c in channels:
            row[f"HR_{c}"] = hr_i[c]
            row[f"SQI_{c}"] = per[c]["sq"]["sqi"]
        rows.append(row)

    return rows, starts, fs_ppg
