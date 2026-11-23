# -*- coding: utf-8 -*-
"""
主运行脚本：加载数据库 → 四通道单通道预处理 → 信号级融合 → 特征级融合，
输出控制台摘要、图表与结果文件到 output/ 目录。

运行：python run_pipeline.py
"""
import os, json, csv
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import signal as sig

import config as cfg
from data_loader import load_ppg, load_accel, load_ecg
import preprocess
import signal_fusion
import feature_fusion
import sliding_window

# ---------------- matplotlib 中文 ----
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 110

OUT = cfg.OUTPUT_DIR


def ecg_reference_hr(ecg, fs):
    """
    由 ECG 提取参考心率(bpm)。
    幅度阈值法(峰 > mean + k*std)在实测中比 prominence 法更稳健，
    避免误检基线伪峰导致心率虚高。返回 (hr, n_R峰)。
    """
    k = 2.5
    th = ecg.mean() + k * ecg.std()
    distance = int(fs * 60.0 / cfg.PEAK["max_hr"])
    pk, _ = sig.find_peaks(ecg, height=th, distance=distance)
    rr = np.diff(pk) / fs
    rr = rr[(rr >= 0.3) & (rr <= 1.5)]
    if len(rr) < 5:
        return float("nan"), int(len(pk))
    hr = 60.0 / np.mean(rr)
    return float(hr), int(len(pk))


def main():
    print("=" * 70)
    print("绿光手环四通道 PPG 预处理与双级别融合管线")
    print("=" * 70)

    # ---------------- 1. 加载数据 ----------------
    print("\n[1] 加载数据 ...")
    ppg, fs_ppg, t_ppg, qc_ppg = load_ppg(cfg.DB_PATH, with_qc=True)
    ax, ay, az, fs_acc, t_acc, qc_acc = load_accel(cfg.DB_PATH, with_qc=True)
    ecg, fs_ecg, t_ecg, qc_ecg = load_ecg(cfg.DB_PATH, with_qc=True)
    print(f"  PPG: {len(ppg[list(ppg)[0]])} 样本 x 4 通道, fs={fs_ppg:.2f} Hz")
    print(f"  ACC: {len(ax)} 样本, fs={fs_acc:.2f} Hz")
    print(f"  ECG: {len(ecg)} 样本, fs={fs_ecg:.2f} Hz")
    # 采样率按额定值、时间轴按包锚点重建，质量报告打出来："实测"明显低于额定
    # 说明设备在丢样本，断点/缺口则说明中途断连过
    for _qc in (qc_ppg, qc_acc, qc_ecg):
        print(_qc.summary())

    # ---------------- 2. 单通道预处理 ----------------
    print("\n[2] 四通道单通道预处理(运动伪影→去基线→带通→异常点) ...")
    pre = {}
    for ch in sorted(ppg):
        d = preprocess.preprocess_channel(ppg[ch], ax, ay, az, fs_ppg, fs_acc)
        pre[ch] = d
        s = d["outlier_stats"]
        print(f"  {ch}: 异常点 {s['n_outlier']}/{s['n_total']} "
              f"({s['outlier_ratio']*100:.2f}%)")

    # ---------------- 3. 信号级融合 ----------------
    print("\n[3] 信号级融合(峰检测→质量→同步→标准化→加权平均) ...")
    sig_result, per_ch = signal_fusion.signal_level_fusion(pre, fs_ppg, fs_acc)
    print("  通道  峰数   MeanPPI(s)  HR(bpm)  SQI    适用融合")
    for ch in sorted(per_ch):
        p = per_ch[ch]
        ok = sig_result["fusion_ok"][ch]
        print(f"  {ch}  {p['n_peaks']:5d}   {p['mean_ppi']:9.3f}   "
              f"{p['hr']:7.2f}   {p['sqi']:.3f}    {'是' if ok else '否'}")
    print("  加权权重(归一化):", {ch: round(w, 3) for ch, w in
                                  sig_result["norm_weights"].items()})

    # ECG 参考心率
    ecg_hr, n_r = ecg_reference_hr(ecg, fs_ecg)
    print(f"  ECG 参考心率: {ecg_hr:.2f} bpm (检出 R 峰 {n_r} 个)")

    # ---------------- 4. 特征级融合 ----------------
    print("\n[4] 特征级融合(各通道 HR/PRV + 拼接/注意力) ...")
    feats = {ch: feature_fusion.extract_features(per_ch[ch]["peaks_idx"], fs_ppg)
             for ch in sorted(per_ch)}
    sqi = {ch: per_ch[ch]["sqi"] for ch in sorted(per_ch)}
    feat_result = feature_fusion.feature_level_fusion(feats, sqi)

    rows = []
    for ch in sorted(feats):
        rows.append({"channel": ch, **feats[ch]})
    print("  通道  HR    MeanPPI  SDNN   RMSSD  LF/HF   SD1/SD2  SampEn")
    for ch in sorted(feats):
        f = feats[ch]
        print(f"  {ch}  {f['HR_bpm']:6.1f} {f['Mean_PPI_ms']:8.1f} "
              f"{f['SDNN_ms']:7.1f} {f['RMSSD_ms']:7.1f} "
              f"{f['LFHF']:7.2f} {f['SD1SD2']:7.2f}  {f['SampEn']:6.2f}")
    print("  注意力权重:", {ch: round(w, 3) for ch, w in
                            feat_result["attention_weights"].items()})
    af = feat_result["attention_fused"]
    print("  注意力融合 HR:", round(af["HR_bpm"], 2), "bpm; "
          "MeanPPI:", round(af["Mean_PPI_ms"], 2), "ms")

    # ---------------- 结果落地 ----------------
    print("\n[5] 写入结果文件到 output/ ...")
    # 汇总 JSON(唯一计算源)
    summary = dict(
        sampling=dict(fs_ppg=float(fs_ppg), fs_acc=float(fs_acc),
                      fs_ecg=float(fs_ecg)),
        preprocess={ch: pre[ch]["outlier_stats"] for ch in pre},
        signal_fusion=dict(
            per_channel={ch: dict(
                n_peaks=per_ch[ch]["n_peaks"],
                mean_ppi=per_ch[ch]["mean_ppi"],
                hr=per_ch[ch]["hr"],
                sqi=per_ch[ch]["sqi"],
                q_reg=per_ch[ch]["q_reg"], q_amp=per_ch[ch]["q_amp"],
                q_cov=per_ch[ch]["q_cov"], q_mot=per_ch[ch]["q_mot"],
                delay_s=per_ch[ch]["delay_s"]) for ch in per_ch},
            norm_weights=sig_result["norm_weights"],
            fusion_ok=sig_result["fusion_ok"],
            ecg_reference_hr=ecg_hr),
        features={ch: feats[ch] for ch in feats},
        attention_weights=feat_result["attention_weights"],
        attention_fused=feat_result["attention_fused"],
    )

    # ---- 完整融合信号导出(t, 融合原始/平滑, 各通道 z-score) ----
    t_ppg = np.arange(len(sig_result["fused_smooth"])) / fs_ppg
    with open(os.path.join(OUT, "fused_signal_full.csv"), "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "fused_raw", "fused_smooth"] +
                   [f"{ch}_z" for ch in sorted(per_ch)])
        for i in range(len(t_ppg)):
            w.writerow([round(t_ppg[i], 6),
                        round(float(sig_result["fused_raw"][i]), 6),
                        round(float(sig_result["fused_smooth"][i]), 6)] +
                       [round(float(per_ch[c]["z"][i]), 6)
                        for c in sorted(per_ch)])
    print(f"  完整融合信号已导出: fused_signal_full.csv "
          f"({len(t_ppg)} 行 x {2 + len(per_ch)} 列)")

    # ---- 滑动窗口特征(窗口 {win}s / 步长 {step}s) ----
    # 记录比默认 5 分钟窗口还短时(现场试采常见)，按记录长度的 60% 收缩窗口，
    # 否则 starts 为空、一个特征窗口都产不出来。
    n_samp = len(pre[sorted(pre)[0]]["clean"])
    dur_s = n_samp / float(fs_ppg)
    win_sec = float(cfg.WINDOW["win_sec"])
    step_sec = float(cfg.WINDOW["step_sec"])
    if dur_s < win_sec * 1.2:
        win_sec = max(20.0, round(dur_s * 0.6, 1))
        step_sec = max(5.0, round(win_sec / 4.0, 1))
        print(f"  [提示] 记录长度仅 {dur_s:.1f}s，滑动窗口收缩为 "
              f"{win_sec}s / 步长 {step_sec}s")
    sl_rows, sl_starts, _ = sliding_window.sliding_window_features(
        pre, fs_ppg, win_sec, step_sec)
    sl_fields = (["window_id", "t_start_s", "t_end_s", "HR_bpm",
                  "Mean_PPI_ms", "SDNN_ms", "RMSSD_ms", "pNN50", "LFHF",
                  "LFnu", "HFnu", "SD1SD2", "SampEn", "HR_fused_sqi"] +
                 [f"HR_{c}" for c in sorted(per_ch)] +
                 [f"SQI_{c}" for c in sorted(per_ch)])
    if sl_rows:
        with open(os.path.join(OUT, "sliding_features.csv"), "w", newline="",
                  encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=sl_fields, extrasaction="ignore")
            w.writeheader()
            for r in sl_rows:
                w.writerow(r)
        print(f"  滑动窗口特征: 窗口 {win_sec}s / 步长 "
              f"{step_sec}s, 共 {len(sl_rows)} 个窗口 "
              f"(0–{sl_rows[-1]['t_end_s']}s)")
        summary["sliding"] = dict(win_sec=win_sec, step_sec=step_sec,
                                  n_windows=len(sl_rows), rows=sl_rows)
    else:
        print("  [跳过] 记录太短，产不出任何完整窗口")

    with open(os.path.join(OUT, "results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=float)

    # CSV
    with open(os.path.join(OUT, "features_per_channel.csv"), "w",
              newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["channel"] +
                           feature_fusion._FEATURE_KEYS)
        w.writeheader()
        for ch in sorted(feats):
            w.writerow({"channel": ch, **feats[ch]})
    with open(os.path.join(OUT, "per_channel_peaks.csv"), "w",
              newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["channel", "n_peaks", "mean_ppi_s", "hr_bpm", "sqi",
                    "q_reg", "q_amp", "q_cov", "q_mot", "delay_s",
                    "fusion_ok"])
        for ch in sorted(per_ch):
            p = per_ch[ch]
            w.writerow([ch, p["n_peaks"], round(p["mean_ppi"], 4),
                        round(p["hr"], 2), round(p["sqi"], 4),
                        round(p["q_reg"], 4), round(p["q_amp"], 4),
                        round(p["q_cov"], 4), round(p["q_mot"], 4),
                        round(p["delay_s"], 4), sig_result["fusion_ok"][ch]])

    # ---------------- 图表 ----------------
    _plot_preprocessing(pre, fs_ppg)
    _plot_peaks(per_ch, pre, fs_ppg)
    _plot_fused(sig_result, per_ch, fs_ppg)
    _plot_ppi_tachogram(per_ch, fs_ppg)
    _plot_poincare(per_ch)
    _plot_spectra(feats, per_ch)
    _plot_weights(sig_result, feat_result)
    if sl_rows:
        _plot_sliding(sl_rows, fs_ppg, win_sec, step_sec)

    print("\n完成。结果文件已写入:", cfg.OUTPUT_DIR)


# ================================================================ 绘图
def _seg_idx(n, fs, t0, t1):
    """把 [t0,t1] 裁到记录范围内；记录比 t1 短时截取靠后的一段。

    原脚本把所有展示窗口写死成 10-40s / 100-120s，遇到 1 分钟左右的
    现场短记录时切片为空，matplotlib 直接抛 "x and y must have same
    first dimension"。
    """
    dur = n / float(fs)
    span = t1 - t0
    if dur <= t0 + span:
        t1 = dur
        t0 = max(0.0, t1 - min(span, dur * 0.6))
    i0 = int(max(0.0, t0) * fs)
    i1 = int(min(dur, t1) * fs)
    i0 = max(0, min(i0, n - 2))
    i1 = max(i0 + 2, min(i1, n))
    return i0, i1

def _plot_preprocessing(pre, fs):
    ch = "PPG2"
    d = pre[ch]
    n = len(d["clean"])
    i0, i1 = _seg_idx(n, fs, 10.0, 40.0)     # 展示 10-40s（短记录自动裁剪）
    t = np.arange(i0, i1) / fs
    fig, ax = plt.subplots(4, 1, figsize=(13, 9), sharex=True)
    labels = ["原始", "去运动伪影后", "去基线+带通后", "异常点修复后"]
    for a, arr, lab in zip(ax, [d["raw"], d["no_artifact"], d["filtered"],
                                d["clean"]], labels):
        a.plot(t, arr[i0:i1], lw=0.7)
        a.set_ylabel(lab, fontsize=9)
        a.grid(alpha=0.3)
    ax[0].set_title(f"单通道预处理各阶段 ({ch}, 10–40s)")
    ax[-1].set_xlabel("时间 (s)")
    # 标记运动段
    y0, y1 = ax[0].get_ylim()
    m = d["motion_mask"]
    ax[0].fill_between(t, y0, y1, where=m[i0:i1], color="r", alpha=0.12,
                       label="运动段")
    ax[0].legend(loc="upper right", fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "preprocessing.png"))
    plt.close(fig)


def _plot_peaks(per_ch, pre, fs):
    n = len(pre[sorted(pre)[0]]["clean"])
    i0, i1 = _seg_idx(n, fs, 100.0, 115.0)
    t = np.arange(i0, i1) / fs
    fig, ax = plt.subplots(len(per_ch), 1, figsize=(13, 9), sharex=True)
    for a, ch in zip(ax, sorted(per_ch)):
        x = pre[ch]["clean"]
        a.plot(t, x[i0:i1], lw=0.6, color="tab:blue")
        pk = per_ch[ch]["peaks_idx"]
        pkm = pk[(pk >= i0) & (pk < i1)]
        a.plot(pkm / fs, x[pkm], "r^", ms=4, label=f"SQI={per_ch[ch]['sqi']:.2f}")
        a.set_ylabel(ch, fontsize=9); a.grid(alpha=0.3)
        a.legend(loc="upper right", fontsize=8)
    ax[0].set_title("四通道脉搏峰检测 (100–115s)")
    ax[-1].set_xlabel("时间 (s)")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "peaks.png"))
    plt.close(fig)


def _plot_fused(sig_result, per_ch, fs):
    n = len(sig_result["fused_smooth"])
    i0, i1 = _seg_idx(n, fs, 100.0, 120.0)
    t = np.arange(i0, i1) / fs
    fig, ax = plt.subplots(len(per_ch) + 1, 1, figsize=(13, 10),
                           sharex=True)
    for a, ch in zip(ax[:-1], sorted(per_ch)):
        a.plot(t, per_ch[ch]["z"][i0:i1], lw=0.6)
        a.set_ylabel(ch + "(z)", fontsize=9); a.grid(alpha=0.3)
    ax[-1].plot(t, sig_result["fused_smooth"][i0:i1], lw=1.2, color="crimson")
    ax[-1].set_ylabel("加权融合", fontsize=9); ax[-1].grid(alpha=0.3)
    ax[0].set_title("幅值标准化(z-score)与信号级加权融合 (100–120s)")
    ax[-1].set_xlabel("时间 (s)")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "fused_signal.png"))
    plt.close(fig)


def _plot_ppi_tachogram(per_ch, fs=250.0):
    fig, ax = plt.subplots(len(per_ch), 1, figsize=(13, 8), sharex=True)
    for a, ch in zip(ax, sorted(per_ch)):
        p = per_ch[ch]
        t = p["peaks_idx"][:-1] / float(fs)
        a.plot(t, p["ppi"] * 1000.0, lw=0.6, marker="o", markersize=1.5)
        a.set_ylabel(ch, fontsize=9); a.grid(alpha=0.3)
    ax[0].set_title("各通道峰间距(PPI) tachogram")
    ax[-1].set_xlabel("时间 (s)"); ax[-1].set_ylabel("PPI (ms)")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "ppi_tachogram.png"))
    plt.close(fig)


def _plot_poincare(per_ch):
    fig, axes = plt.subplots(2, 2, figsize=(10, 9))
    for ax, ch in zip(axes.ravel(), sorted(per_ch)):
        ppi = per_ch[ch]["ppi"] * 1000.0
        ax.scatter(ppi[:-1], ppi[1:], s=3, alpha=0.4)
        lim = [np.percentile(ppi, 1), np.percentile(ppi, 99)]
        ax.plot(lim, lim, "--", color="gray", lw=0.8)
        ax.set_xlim(lim); ax.set_ylim(lim)
        ax.set_xlabel("PPI(n) (ms)"); ax.set_ylabel("PPI(n+1) (ms)")
        ax.set_title(ch, fontsize=10); ax.grid(alpha=0.3)
    fig.suptitle("各通道 Poincaré 图")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "poincare.png"))
    plt.close(fig)


def _plot_spectra(feats, per_ch):
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    for a, ch in zip(ax.ravel(), sorted(feats)):
        ppi = per_ch[ch]["ppi"] * 1000.0
        t = np.cumsum(np.r_[0.0, per_ch[ch]["ppi"]])
        tq = np.arange(0, t[-1], 1.0 / cfg.PRV_FREQ["resample_hz"])
        x = np.interp(tq, t[:-1], ppi)
        x = x - np.mean(x)
        f, P = sig.welch(x, fs=cfg.PRV_FREQ["resample_hz"],
                         nperseg=min(256, len(x)))
        a.semilogy(f, P, lw=0.8)
        a.axvspan(*cfg.PRV_FREQ["lf"], color="tab:blue", alpha=0.15)
        a.axvspan(*cfg.PRV_FREQ["hf"], color="tab:orange", alpha=0.15)
        a.set_xlim(0, 0.5)
        a.set_xlabel("频率 (Hz)"); a.set_ylabel("PSD"); a.grid(alpha=0.3)
        a.set_title(ch, fontsize=10)
    fig.suptitle("各通道 PPI 频谱(LF/HF 带标出)")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "spectra.png"))
    plt.close(fig)


def _plot_weights(sig_result, feat_result):
    chs = sorted(sig_result["norm_weights"])
    fig, ax = plt.subplots(1, 2, figsize=(11, 4))
    w1 = [sig_result["norm_weights"][c] for c in chs]
    w2 = [feat_result["attention_weights"][c] for c in chs]
    x = np.arange(len(chs))
    ax[0].bar(x, w1, color="tab:blue")
    ax[0].set_xticks(x); ax[0].set_xticklabels(chs)
    ax[0].set_title("信号级融合权重(SQI归一化)")
    ax[1].bar(x, w2, color="tab:green")
    ax[1].set_xticks(x); ax[1].set_xticklabels(chs)
    ax[1].set_title("特征级注意力权重(softmax SQI)")
    for a in ax: a.set_ylim(0, 1); a.grid(alpha=0.3, axis="y")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "weights.png"))
    plt.close(fig)


def _plot_sliding(sl_rows, fs_ppg, win_sec=None, step_sec=None):
    """滑动窗口特征趋势图(HR / SDNN / RMSSD / LFHF / SampEn / 平均 SQI)。"""
    t = [r["t_start_s"] for r in sl_rows]
    panels = [
        ("HR_bpm", "融合心率 (bpm)", None),
        ("RMSSD_ms", "RMSSD (ms)", None),
        ("SDNN_ms", "SDNN (ms)", None),
        ("LFHF", "LF/HF", None),
        ("SampEn", "样本熵", None),
        ("HR_PPG1", "各通道心率 (bpm)", [f"HR_{c}" for c in
                                          sorted({k[3:] for k in
                                                  sl_rows[0] if k.startswith("HR_")})]),
    ]
    n = len(panels)
    fig, axes = plt.subplots(n, 1, figsize=(13, 2.2 * n), sharex=True)
    for a, (key, ylab, cols) in zip(axes, panels):
        if cols:
            for c in cols:
                a.plot(t, [r[c] for r in sl_rows], marker="o", ms=3,
                       lw=1.2, label=c)
            a.legend(fontsize=8, ncol=4)
        else:
            a.plot(t, [r[key] for r in sl_rows], marker="o", ms=3,
                   lw=1.4, color="crimson")
        a.set_ylabel(ylab, fontsize=9); a.grid(alpha=0.3)
    axes[0].set_title("滑动窗口特征趋势 "
                      f"(窗口 {win_sec if win_sec is not None else cfg.WINDOW['win_sec']}s"
                      f" / 步长 {step_sec if step_sec is not None else cfg.WINDOW['step_sec']}s)")
    axes[-1].set_xlabel("窗口起始时间 (s)")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "sliding_trends.png"))
    plt.close(fig)


if __name__ == "__main__":
    main()
