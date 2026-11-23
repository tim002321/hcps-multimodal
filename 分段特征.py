"""从分段库提取心电 / 手环特征，按被试标准化后看各实验阶段的分布。

输入
    实验分段\\<姓名>\\<组别>\\T??_HHMM-HHMM\\trial.db
    实验分段\\实验分段.db（任务清单与设计变量）

输出
    实验分段\\特征表.csv         一次任务一行，原始值 + 设计变量 + 可用性
    实验分段\\特征_归一化.csv     同上，数值列换成被试内 z 分数
    实验分段\\图1_各阶段分布.png   9 个特征的箱线图（按条件类型分组）
    实验分段\\图2_条件均值热图.png 条件 × 特征的均值 z 热图
    实验分段\\图3_被试原始差异.png 标准化前的被试差异（说明为什么要标准化）

为什么要按被试标准化
    三位被试的基线心率差 10~15 bpm（曹超 82、马国庆 85、高晨瑞 86 上下），
    不消除个体差异的话，"条件间的差别"会被"人的差别"淹掉。
    z = (x - 该被试所有段的均值) / 该被试所有段的标准差，只去掉个体水平与尺度，
    不改变条件之间的相对关系。

用法
    python 分段特征.py
"""

import csv
import glob
import json
import os
import sqlite3

import numpy as np
from scipy import signal as sg

from ppg_pipeline.data_loader import timeline_from_rows, FS_ECG, FS_GSR

ROOT = os.path.dirname(os.path.abspath(__file__))
SEG = os.path.join(ROOT, "实验分段")
KIND_ORDER = ["基线", "低能见度", "侧风", "复合"]   # 单发失效已并入复合
SUBJECT_COLORS = {"晁超": "#c0504d", "马国庆": "#4f81bd", "高晨瑞": "#9bbb59"}

# 参与画图的特征（名字 → 中文标题）
PLOT_FEATURES = [
    ("hr_mean", "平均心率 (bpm)"),
    ("hr_sd", "心率标准差 (bpm)"),
    ("sdnn", "SDNN (ms)"),
    ("rmssd", "RMSSD (ms)"),
    ("lf_hf", "LF/HF"),
    ("hr_slope", "段内心率斜率 (bpm/min)"),
    ("acc_active", "活动占比"),
    ("scr_rate", "皮电反应 (次/min)"),
    ("temp_mean", "体温 (℃)"),
]


# ---------------------------------------------------------------- 读任务清单
def load_trials():
    con = sqlite3.connect(os.path.join(SEG, "实验分段.db"))
    cols = ("id", "subject", "grp", "task", "kind", "start", "end", "duration_min",
            "setting", "note", "vis_m", "wind_dir", "wind_speed", "failure",
            "failure_phase")
    rows = [dict(zip(cols, r)) for r in con.execute(
        "select %s from trials order by id" % ",".join(cols))]
    con.close()
    return rows


def trial_db_path(trial):
    base = os.path.join(SEG, trial["subject"], trial["grp"])
    for folder in glob.glob(os.path.join(base, "T%02d_*" % trial["task"])):
        path = os.path.join(folder, "trial.db")
        if os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------- 特征
def _series(con, table):
    return con.execute('select time, value from "%s" order by rowid' % table).fetchall()


def ecg_features(con):
    rows = _series(con, "ecg_data")
    out = dict(ecg_rows=len(rows), hr_mean=np.nan, hr_sd=np.nan, hr_min=np.nan,
               hr_max=np.nan, sdnn=np.nan, rmssd=np.nan, pnn50=np.nan, lf_hf=np.nan,
               hr_slope=np.nan, hr_drift=np.nan, ecg_valid=0)
    if len(rows) < 500:
        return out

    v = np.array([float(r[1]) for r in rows])
    # 采样率按额定值、时间轴按包锚点重建（见 ppg_pipeline/data_loader.py 的说明）。
    # 以前这里是 (行数-1)/墙钟跨度，遇到整包共用时间戳、中途断连就会跑偏。
    t, qc = timeline_from_rows(rows, FS_ECG, "ecg_data")
    fs = qc.fs
    span = qc.duration_wall

    if v.std() < 5.0:                       # 电极没接触：只剩 ±1 噪声
        return out
    hi = min(40.0, fs * 0.45)
    if hi <= 3.5:
        return out
    y = sg.filtfilt(*sg.butter(2, [3 / (fs / 2), hi / (fs / 2)], btype="band"),
                    sg.detrend(v))
    if float(np.percentile(y, 99) - np.percentile(y, 1)) < 100:
        return out
    pk, _ = sg.find_peaks(y, prominence=1.0 * y.std(), distance=int(fs * 0.33))
    if len(pk) < 10:
        return out

    # 用布尔掩码剔除漏检/多检造成的离群间期，同时保住"哪个间期对应哪两个峰"，
    # 否则后面算段内趋势时峰和间期对不上（这一步写错会让 hr_slope 全是空值）。
    rr_all = np.diff(pk) / fs
    mask = np.ones(len(rr_all), dtype=bool)
    for _ in range(4):
        med = np.median(rr_all[mask])
        new = (rr_all > 0.55 * med) & (rr_all < 1.6 * med)
        if new.sum() < 10:
            break
        if np.array_equal(new, mask):
            break
        mask = new
    rr = rr_all[mask]
    if len(rr) < 10:
        return out
    t_beat = t[pk[1:][mask]]

    hr = 60.0 / rr
    cv = rr.std() / rr.mean()
    covered = rr.sum() / max(span, 1e-9)
    if not (45 <= np.median(hr) <= 150) or cv > 0.25 or covered < 0.5:
        return out

    out.update(ecg_valid=1, hr_mean=float(hr.mean()), hr_sd=float(hr.std()),
               hr_min=float(np.percentile(hr, 5)), hr_max=float(np.percentile(hr, 95)))
    rrs = rr * 1000.0
    out["sdnn"] = float(np.std(rrs, ddof=1))
    out["rmssd"] = float(np.sqrt(np.mean(np.diff(rrs) ** 2)))
    out["pnn50"] = float(100.0 * np.mean(np.abs(np.diff(rrs)) > 50))

    # 段内趋势：每个心搏的时刻 ↔ 瞬时心率做最小二乘，单位 bpm/min
    if len(t_beat) == len(hr) and len(hr) > 5:
        minutes = (t_beat - t_beat[0]) / 60.0
        if minutes[-1] > 0.5:
            out["hr_slope"] = float(np.polyfit(minutes, hr, 1)[0])
            third = max(1, len(hr) // 3)
            out["hr_drift"] = float(hr[-third:].mean() - hr[:third].mean())

    if len(rrs) >= 60:                      # 频域：4Hz 重采样后 Welch
        t_rr = np.cumsum(rr)
        grid = np.arange(0, t_rr[-1], 0.25)
        if len(grid) > 16:
            interp = np.interp(grid, t_rr[1:], rrs[1:])
            f, p = sg.welch(interp - interp.mean(), fs=4.0, nperseg=min(256, len(interp)))
            lf = p[(f >= 0.04) & (f < 0.15)].sum()
            hf = p[(f >= 0.15) & (f < 0.4)].sum()
            if hf > 0:
                out["lf_hf"] = float(lf / hf)
    return out


def acc_features(con):
    out = dict(acc_rows=0, acc_mean=np.nan, acc_sd=np.nan, acc_active=np.nan)
    rows = _series(con, "bracelet_accel_data")
    vals = []
    for _, raw in rows:
        try:
            d = json.loads(raw)
            vals.append((d["x"], d["y"], d["z"]))
        except Exception:                    # noqa: BLE001
            pass
    if len(vals) < 50:
        return out
    arr = np.array(vals, dtype=float)
    mag = np.linalg.norm(arr, axis=1)
    dev = np.abs(mag - np.median(mag))
    out.update(acc_rows=len(arr), acc_mean=float(mag.mean()), acc_sd=float(mag.std()),
               acc_active=float(np.mean(dev > 0.05)))
    return out


def gsr_features(con):
    out = dict(gsr_rows=0, scl_mean=np.nan, scl_range=np.nan, scr_count=np.nan,
               scr_rate=np.nan, gsr_valid=0)
    rows = _series(con, "bracelet_gsr_data")
    if len(rows) < 50:
        return out
    g = np.array([float(r[1]) for r in rows])
    t, qc = timeline_from_rows(rows, FS_GSR, "bracelet_gsr_data")
    span = max(qc.duration_wall, 1e-9)
    fs = qc.fs
    out.update(gsr_rows=len(g), scl_mean=float(g.mean()),
               scl_range=float(np.percentile(g, 95) - np.percentile(g, 5)))
    if g.std() < 1e-4:
        return out
    if len(g) > 100:                         # 纯线性放电 → 电极浮空，不是皮肤电
        k = np.polyfit(np.arange(len(g)) / fs, g, 1)
        resid = g - np.polyval(k, np.arange(len(g)) / fs)
        if abs(k[0]) > 1e-5 and resid.std() < 0.01 * (g.max() - g.min() + 1e-9):
            return out
    out["gsr_valid"] = 1
    if fs >= 2:
        smooth = sg.filtfilt(*sg.butter(2, min(0.5, fs * 0.4) / (fs / 2)), g - g.mean())
        pk, _ = sg.find_peaks(smooth, prominence=0.01, distance=int(fs * 1.0))
        out["scr_count"] = len(pk)
        minutes = span / 60.0
        out["scr_rate"] = float(len(pk) / minutes) if minutes > 0.2 else np.nan
    return out


def temp_features(con):
    rows = _series(con, "temperature_data")
    if len(rows) < 20:
        return dict(temp_rows=0, temp_mean=np.nan, temp_range=np.nan)
    v = np.array([float(r[1]) for r in rows])
    return dict(temp_rows=len(v), temp_mean=float(v.mean()),
                temp_range=float(np.percentile(v, 95) - np.percentile(v, 5)))


# ---------------------------------------------------------------- 标准化与出图
def zscore_by_subject(rows, columns):
    for subject in {r["subject"] for r in rows}:
        sub = [r for r in rows if r["subject"] == subject]
        for col in columns:
            vals = [r[col] for r in sub if r.get(col) is not None and not np.isnan(r[col])]
            if len(vals) < 3:
                continue
            mu, sd = float(np.mean(vals)), float(np.std(vals, ddof=1))
            if sd < 1e-9:
                continue
            for r in sub:
                v = r.get(col)
                r[col + "_z"] = None if v is None or np.isnan(v) else (v - mu) / sd
    return rows


def plot_distributions(rows, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(3, 3, figsize=(14, 11))
    for ax, (col, title) in zip(axes.ravel(), PLOT_FEATURES):
        data, positions, colors = [], [], []
        for i, kind in enumerate(KIND_ORDER):
            vals = [r.get(col + "_z") for r in rows
                    if r["kind"] == kind and r.get(col + "_z") is not None]
            if len(vals) >= 2:
                data.append(vals)
                positions.append(i)
                colors.append("#dce6f1")
        if not data:
            ax.set_visible(False)
            continue
        bp = ax.boxplot(data, positions=positions, widths=0.55, patch_artist=True,
                        showfliers=False, medianprops=dict(color="#1f4e79", lw=2))
        for patch in bp["boxes"]:
            patch.set_facecolor("#dce6f1")
            patch.set_edgecolor("#7f7f7f")
        # 每个点是一个任务段，按被试上色
        for i, kind in enumerate(KIND_ORDER):
            for subject, color in SUBJECT_COLORS.items():
                pts = [r.get(col + "_z") for r in rows
                       if r["kind"] == kind and r["subject"] == subject
                       and r.get(col + "_z") is not None]
                if not pts:
                    continue
                jitter = (np.random.RandomState(hash(kind) % 97 + len(pts)).rand(len(pts)) - 0.5) * 0.22
                ax.scatter(np.full(len(pts), i) + jitter, pts, s=22, alpha=0.8,
                           color=color, edgecolors="white", linewidths=0.4,
                           label=subject if (i == 0 and col == PLOT_FEATURES[0][0]) else None)
        ax.axhline(0, color="#999999", lw=0.8, ls="--")
        ax.set_xticks(range(len(KIND_ORDER)))
        ax.set_xticklabels(KIND_ORDER, fontsize=9)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel("被试内 z 分数", fontsize=8)
        ax.grid(alpha=0.25, axis="y")
        ax.tick_params(labelsize=8)
    handles = [plt.Line2D([], [], marker="o", ls="", color=c, label=s)
               for s, c in SUBJECT_COLORS.items()]
    fig.legend(handles=handles, loc="upper right", ncol=3, frameon=False, fontsize=9)
    fig.suptitle("各实验阶段的心电 / 手环特征分布（按被试标准化，点=单次飞行）", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_png, dpi=140)
    return out_png


def plot_heatmap(rows, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    matrix, counts = [], []
    for col, _title in PLOT_FEATURES:
        line, cnt = [], []
        for kind in KIND_ORDER:
            vals = [r.get(col + "_z") for r in rows
                    if r["kind"] == kind and r.get(col + "_z") is not None]
            line.append(np.mean(vals) if vals else np.nan)
            cnt.append(len(vals))
        matrix.append(line)
        counts.append(cnt)
    matrix = np.array(matrix)

    fig, ax = plt.subplots(figsize=(8.5, 6.2))
    im = ax.imshow(matrix, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(KIND_ORDER)))
    ax.set_xticklabels(KIND_ORDER)
    ax.set_yticks(range(len(PLOT_FEATURES)))
    ax.set_yticklabels([t for _c, t in PLOT_FEATURES], fontsize=9)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            v = matrix[i, j]
            if np.isnan(v):
                ax.text(j, i, "—", ha="center", va="center", fontsize=9, color="#888888")
                continue
            ax.text(j, i, "%.2f\n(n=%d)" % (v, counts[i][j]), ha="center", va="center",
                    fontsize=8, color="white" if abs(v) > 0.6 else "#222222")
    ax.set_title("各阶段特征均值（被试内 z 分数，括号内为该条件有效段数）", fontsize=11)
    fig.colorbar(im, ax=ax, shrink=0.82, label="均值 z")
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    return out_png


def plot_subject_raw(rows, out_png):
    """标准化之前：同一个人跨条件、不同人之间的原始差别。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, (col, title) in zip(axes, (("hr_mean", "平均心率 (bpm)"), ("rmssd", "RMSSD (ms)"))):
        for subject, color in SUBJECT_COLORS.items():
            xs, ys = [], []
            for i, kind in enumerate(KIND_ORDER):
                vals = [r[col] for r in rows
                        if r["subject"] == subject and r["kind"] == kind
                        and r.get(col) is not None and not np.isnan(r[col])]
                if vals:
                    xs.append(i)
                    ys.append(np.mean(vals))
                    ax.scatter(np.full(len(vals), i) + (list(SUBJECT_COLORS).index(subject) - 1) * 0.18,
                               vals, s=18, alpha=0.55, color=color)
            if xs:
                ax.plot(xs, ys, "-o", color=color, lw=1.6, ms=5, label=subject)
        ax.set_xticks(range(len(KIND_ORDER)))
        ax.set_xticklabels(KIND_ORDER, fontsize=9)
        ax.set_title(title + "（原始值）", fontsize=10)
        ax.grid(alpha=0.25, axis="y")
    axes[0].legend(fontsize=8, frameon=False)
    fig.suptitle("标准化前：被试间基线差异明显，条件差异被个体差异掩盖", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out_png, dpi=140)
    return out_png


def plot_subject_condition(rows, out_png):
    """被试 × 条件 的均值 z 矩阵：一眼看出哪些格子只有一个人。

    标准化消除了个体水平，但消除不了"某个条件只有某个人飞过"这种设计混淆，
    这张图就是用来暴露它的。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    subjects = sorted({r["subject"] for r in rows})
    panels = (("hr_mean", "平均心率"), ("rmssd", "RMSSD"))
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.6))
    for ax, (col, title) in zip(axes, panels):
        mat, cnt = [], []
        for subject in subjects:
            line, c = [], []
            for kind in KIND_ORDER:
                vals = [r.get(col + "_z") for r in rows
                        if r["subject"] == subject and r["kind"] == kind
                        and r.get(col + "_z") is not None]
                line.append(np.mean(vals) if vals else np.nan)
                c.append(len(vals))
            mat.append(line)
            cnt.append(c)
        mat = np.array(mat)
        im = ax.imshow(mat, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
        ax.set_xticks(range(len(KIND_ORDER)))
        ax.set_xticklabels(KIND_ORDER, fontsize=9)
        ax.set_yticks(range(len(subjects)))
        ax.set_yticklabels(subjects, fontsize=9)
        for i in range(mat.shape[0]):
            for j in range(mat.shape[1]):
                v = mat[i, j]
                ax.text(j, i, "—" if np.isnan(v) else "%+.2f\nn=%d" % (v, cnt[i][j]),
                        ha="center", va="center", fontsize=8,
                        color="white" if (not np.isnan(v) and abs(v) > 0.6) else "#333333")
        ax.set_title(title + "（被试内 z 均值）", fontsize=10)
        fig.colorbar(im, ax=ax, shrink=0.85)
    fig.suptitle("被试 × 条件覆盖：「复合」已含单发失效（晁超 3 / 马国庆 1 / 高晨瑞 2），"
                 "每格样本极少，格内差异不能当条件效应",
                 fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.9])
    fig.savefig(out_png, dpi=140)
    return out_png


# ---------------------------------------------------------------- 主流程
def main():
    trials = load_trials()
    rows = []
    for t in trials:
        path = trial_db_path(t)
        if path is None:
            continue
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        feats = {}
        feats.update(ecg_features(con))
        feats.update(acc_features(con))
        feats.update(gsr_features(con))
        feats.update(temp_features(con))
        con.close()

        row = {k: t[k] for k in ("id", "subject", "grp", "task", "kind", "start",
                                 "end", "duration_min", "setting", "note",
                                 "vis_m", "wind_dir", "wind_speed", "failure")}
        row.update(feats)
        row["ecd_valid"] = row.pop("ecg_valid")
        rows.append(row)

    # 表里放 None 好过 NaN（csv 里 nan 会变成字符串）
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and np.isnan(v):
                r[k] = None

    numeric = [k for k in rows[0] if k in ("hr_mean", "hr_sd", "hr_min", "hr_max",
                                           "sdnn", "rmssd", "pnn50", "lf_hf",
                                           "hr_slope", "hr_drift", "acc_mean",
                                           "acc_sd", "acc_active", "scl_mean",
                                           "scl_range", "scr_count", "scr_rate",
                                           "temp_mean", "temp_range")]
    zscore_by_subject(rows, numeric)

    fields = (["id", "subject", "grp", "task", "kind", "start", "end", "duration_min",
               "vis_m", "wind_dir", "wind_speed", "failure", "note"]
              + numeric + [c + "_z" for c in numeric] + ["ecg_rows", "acc_rows",
                                                          "gsr_rows", "ecd_valid"])
    raw_csv = os.path.join(SEG, "特征表.csv")
    with open(raw_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})

    z_fields = (["id", "subject", "grp", "task", "kind", "duration_min"]
                + [c + "_z" for c in numeric])
    z_csv = os.path.join(SEG, "特征_归一化.csv")
    with open(z_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=z_fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(r[k], 3) if isinstance(r.get(k), float) else r.get(k))
                        for k in z_fields})

    p1 = plot_distributions(rows, os.path.join(SEG, "图1_各阶段分布.png"))
    p2 = plot_heatmap(rows, os.path.join(SEG, "图2_条件均值热图.png"))
    p3 = plot_subject_raw(rows, os.path.join(SEG, "图3_被试原始差异.png"))
    p4 = plot_subject_condition(rows, os.path.join(SEG, "图4_被试与条件覆盖.png"))

    # 终端摘要
    print("任务段 %d，心电可用 %d，手环可用 %d"
          % (len(rows), sum(1 for r in rows if r["ecd_valid"]),
             sum(1 for r in rows if r["acc_rows"] > 0)))
    print("\n各条件（被试内 z 均值，n=有效段数）：")
    head = "%-8s" % "条件" + "".join("%10s" % c for c, _ in PLOT_FEATURES[:6])
    print(head)
    for kind in KIND_ORDER:
        line = "%-8s" % kind
        for col, _t in PLOT_FEATURES[:6]:
            vals = [r.get(col + "_z") for r in rows
                    if r["kind"] == kind and r.get(col + "_z") is not None]
            line += "%10s" % ("%+.2f(%d)" % (np.mean(vals), len(vals)) if vals else "—")
        print(line)
    print("\n输出：\n  %s\n  %s\n  %s\n  %s\n  %s\n  %s"
          % (raw_csv, z_csv, p1, p2, p3, p4))


if __name__ == "__main__":
    main()
