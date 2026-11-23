# -*- coding: utf-8 -*-
"""各任务组内，各测量指标随任务推进的变化（折线图）。

数据单位说明：一组 = 一个架次块，块内基线任务与特殊条件任务交替出现
（例如晁超第一组 T01 基线 / T03 低能见度 / T06 低能见度 / T08 低能见度）。
因此横轴用「组内任务序号」，点用形状区分条件：
    ○ = 基线任务      △ = 特殊条件任务（低能见度 / 侧风 / 复合）
趋势线只对 ○ 基线点做最小二乘，这样斜率反映的是"单纯随时间累积"，
不会被特殊条件的高值拉偏。

输出（实验分段/）：
    图7_第一组_指标时程.png   ~ 图9_第三组_指标时程.png
    图10_基线段趋势斜率.png
    时程趋势.csv
"""
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SEG = os.path.join(ROOT, "实验分段")

GROUPS = ["第一组", "第二组", "第三组"]
GROUP_FILES = {"第一组": "图7_第一组_指标时程.png",
               "第二组": "图8_第二组_指标时程.png",
               "第三组": "图9_第三组_指标时程.png"}
SUBJECTS = ["晁超", "马国庆", "高晨瑞"]
SUBJECT_COLORS = {"晁超": "#c0504d", "马国庆": "#4f81bd", "高晨瑞": "#9bbb59"}

# (列名, 显示名, 单位)  —— 19 项测量
FEATURES = [
    ("hr_mean", "平均心率", "bpm"),
    ("hr_sd", "心率标准差", "bpm"),
    ("hr_min", "心率 P5", "bpm"),
    ("hr_max", "心率 P95", "bpm"),
    ("sdnn", "SDNN", "ms"),
    ("rmssd", "RMSSD", "ms"),
    ("pnn50", "pNN50", "%"),
    ("lf_hf", "LF/HF", ""),
    ("hr_slope", "段内心率斜率", "bpm/min"),
    ("hr_drift", "段内前后心率差", "bpm"),
    ("acc_mean", "加速度均值", ""),
    ("acc_sd", "加速度标准差", ""),
    ("acc_active", "活动占比", "%"),
    ("scl_mean", "皮电 SCL 均值", ""),
    ("scl_range", "皮电 SCL 极差", ""),
    ("scr_count", "皮电反应次数", "次"),
    ("scr_rate", "皮电反应频率", "次/min"),
    ("temp_mean", "体温", "℃"),
    ("temp_range", "体温极差", "℃"),
]

MIN_BASE = 3          # 少于此数不拟合趋势


def load_rows():
    rows = list(csv.DictReader(open(os.path.join(SEG, "特征表.csv"),
                                    encoding="utf-8-sig")))
    for row in rows:
        row["task"] = int(row["task"])
        row["duration_min"] = float(row["duration_min"] or 0)
        for col, _n, _u in FEATURES:
            raw = row.get(col)
            try:
                row[col] = float(raw) if raw not in (None, "") else np.nan
            except ValueError:
                row[col] = np.nan
    return rows


def series_of(rows, subject, group, col):
    """返回 (任务号数组, 值数组, 是否基线数组)。"""
    sub = sorted([r for r in rows if r["subject"] == subject and r["grp"] == group],
                 key=lambda r: r["task"])
    x = np.array([r["task"] for r in sub], dtype=float)
    y = np.array([r[col] for r in sub], dtype=float)
    base = np.array([r["kind"] == "基线" for r in sub])
    return x, y, base


def fit_baseline(x, y, base):
    """只用基线点拟合，返回 (斜率, 截距, 相关系数) 或 None。"""
    m = base & np.isfinite(y)
    if m.sum() < MIN_BASE:
        return None
    xs, ys = x[m], y[m]
    if np.ptp(xs) == 0 or np.ptp(ys) == 0:
        return None
    slope, intercept = np.polyfit(xs, ys, 1)
    r = np.corrcoef(xs, ys)[0, 1] if len(xs) > 2 else np.nan
    return float(slope), float(intercept), float(r)


def fit_all(x, y):
    """对全部任务点拟合（时间 + 条件混合效应），返回 (斜率, 截距, r)。"""
    m = np.isfinite(y)
    if m.sum() < 3:
        return None
    xs, ys = x[m], y[m]
    if np.ptp(xs) == 0 or np.ptp(ys) == 0:
        return None
    slope, intercept = np.polyfit(xs, ys, 1)
    r = np.corrcoef(xs, ys)[0, 1] if len(xs) > 2 else np.nan
    return float(slope), float(intercept), float(r)


def draw_figure(rows, group):
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    ncol, nrow = 4, 5
    fig, axes = plt.subplots(nrow, ncol, figsize=(15.4, 11.6))
    axes = axes.ravel()
    subjects_here = [s for s in SUBJECTS
                     if any(r["subject"] == s and r["grp"] == group for r in rows)]

    for idx, (col, title, unit) in enumerate(FEATURES):
        ax = axes[idx]
        for subject in subjects_here:
            x, y, base = series_of(rows, subject, group, col)
            if not np.isfinite(y).any():
                continue
            color = SUBJECT_COLORS[subject]
            ax.plot(x, y, "-", color=color, lw=1.35, alpha=0.9, zorder=2)
            ax.plot(x[base], y[base], "o", color=color, ms=4.2,
                    mec="white", mew=0.6, zorder=3)
            ax.plot(x[~base], y[~base], "^", color=color, ms=5.0,
                    mec="white", mew=0.6, zorder=3)
            fit = fit_all(x, y)
            if fit:
                slope, intercept, _r = fit
                xs = np.linspace(x.min(), x.max(), 2)
                ax.plot(xs, slope * xs + intercept, "--", color=color,
                        lw=1.0, alpha=0.7, zorder=1)
        ax.set_title("%s (%s)" % (title, unit) if unit else title, fontsize=9, pad=4)
        ax.grid(alpha=0.25, lw=0.6)
        ax.tick_params(labelsize=7.5)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_xlabel("组内任务序号", fontsize=7.5, labelpad=1)
        if len(subjects_here) < len(SUBJECTS):
            pass

    for j in range(len(FEATURES), nrow * ncol):
        axes[j].axis("off")

    for ax in axes[len(FEATURES):]:
        ax.axis("off")

    handles = [Line2D([], [], color=SUBJECT_COLORS[s], lw=1.6, marker="o",
                      ms=4.5, mec="white", label=s) for s in subjects_here]
    handles += [Line2D([], [], color="#5F5E5A", lw=0, marker="o", ms=5,
                       mec="white", label="基线任务"),
                Line2D([], [], color="#5F5E5A", lw=0, marker="^", ms=5.5,
                       mec="white", label="特殊条件"),
                Line2D([], [], color="#5F5E5A", lw=1.1, ls="--", label="趋势（最小二乘）")]
    fig.legend(handles=handles, loc="upper center", ncol=len(handles),
               frameon=False, fontsize=9.5, bbox_to_anchor=(0.5, 0.972),
               handletextpad=0.5, columnspacing=1.6)

    hours = {}
    for subject in subjects_here:
        sub = [r for r in rows if r["subject"] == subject and r["grp"] == group]
        hours[subject] = (len(sub), sum(r["duration_min"] for r in sub))
    fig.suptitle("%s：各测量指标随任务推进的变化" % group, fontsize=13.5, y=0.990)
    fig.tight_layout(rect=[0, 0, 1, 0.945])
    out = os.path.join(SEG, GROUP_FILES[group])
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def draw_slope_summary(records, key, title, fname, note):
    """三组 × 19 指标的趋势斜率，换算成「每次任务移动多少个标准差」。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(1, 3, figsize=(15.0, 8.2), sharey=True)
    matrices = []
    for group in GROUPS:
        mat = np.full((len(FEATURES), len(SUBJECTS)), np.nan)
        for i, (col, _t, _u) in enumerate(FEATURES):
            for j, subject in enumerate(SUBJECTS):
                rec = records.get((group, subject, col))
                if rec and rec.get(key + "_z") is not None:
                    mat[i, j] = rec[key + "_z"]
        matrices.append(mat)

    vmax = 0.5
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad("#EDEBE6")
    for k, (group, mat) in enumerate(zip(GROUPS, matrices)):
        ax = axes[k]
        im = ax.imshow(np.ma.masked_invalid(mat), cmap=cmap, vmin=-vmax, vmax=vmax,
                       aspect="auto")
        ax.set_xticks(range(len(SUBJECTS)))
        ax.set_xticklabels(SUBJECTS, fontsize=10)
        ax.set_yticks(range(len(FEATURES)))
        if k == 0:
            ax.set_yticklabels([t for _c, t, _u in FEATURES], fontsize=9)
        else:
            # sharey=True 时共享同一批 Tick 对象，调 set_yticklabels([]) 会
            # 把第一张子图的标签一起清掉，只能关掉本轴的标签显示
            ax.tick_params(labelleft=False)
        for i in range(mat.shape[0]):
            for j in range(mat.shape[1]):
                v = mat[i, j]
                if np.isnan(v):
                    ax.text(j, i, "—", ha="center", va="center", fontsize=9,
                            color="#888780")
                else:
                    rec = records.get((group, SUBJECTS[j], FEATURES[i][0]))
                    n = rec["n_all"] if rec else 0
                    label = "%+.2f" % v + ("*" if n < 5 else "")
                    ax.text(j, i, label, ha="center", va="center", fontsize=8,
                            color="white" if abs(v) > 0.32 else "#222222")
        ax.set_title(group, fontsize=11.5, pad=8)
        ax.set_xticks(np.arange(-0.5, len(SUBJECTS), 1), minor=True)
        ax.set_yticks(np.arange(-0.5, len(FEATURES), 1), minor=True)
        ax.grid(which="minor", color="white", lw=1.2)
        ax.tick_params(which="minor", length=0)

    cax = fig.add_axes([0.915, 0.28, 0.012, 0.40])
    plt.colorbar(im, cax=cax,
                 label="每次任务的标准差变化量（%s）"
                       % ("全部任务" if key == "slope" else "仅基线"))
    # 按要求不加大标题和解释行，只留数据本体（组名、行标签、色标）
    fig.subplots_adjust(left=0.135, right=0.90, top=0.93, bottom=0.05, wspace=0.08)
    out = os.path.join(SEG, fname)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def main():
    rows = load_rows()
    records = {}

    for group in GROUPS:
        out = draw_figure(rows, group)
        print("  %s" % out)
        for subject in SUBJECTS:
            sub = [r for r in rows if r["subject"] == subject and r["grp"] == group]
            if not sub:
                continue
            for col, title, unit in FEATURES:
                x, y, base = series_of(rows, subject, group, col)
                sd = np.nanstd(y) if np.isfinite(y).any() else np.nan
                usable = bool(np.isfinite(sd) and sd > 0)
                rec = dict(title=title, unit=unit,
                           n_all=int(np.isfinite(y).sum()),
                           n_base=int((base & np.isfinite(y)).sum()),
                           slope=None, slope_z=None, r=None,
                           b_slope=None, b_slope_z=None, b_r=None)
                fa = fit_all(x, y)
                if fa:
                    rec["slope"], _i, rec["r"] = fa
                    if usable:
                        rec["slope_z"] = rec["slope"] / sd
                fb = fit_baseline(x, y, base)
                if fb:
                    rec["b_slope"], _i, rec["b_r"] = fb
                    if usable:
                        rec["b_slope_z"] = rec["b_slope"] / sd
                records[(group, subject, col)] = rec

    out = draw_slope_summary(
        records, "slope",
        "趋势斜率 · 全部任务（基线 + 特殊条件）",
        "图10_趋势斜率_全部任务.png",
        "正=随任务推进升高，负=降低；单位换算为「每次任务移动几个标准差」；"
        "— ＝有效点不足 3 个；* ＝参与拟合的点少于 5 个，斜率不稳")
    print("  %s" % out)

    out = draw_slope_summary(
        records, "b_slope",
        "趋势斜率 · 仅基线任务（排除条件混淆）",
        "图11_趋势斜率_仅基线.png",
        "只对基线任务拟合，反映单纯的时间累积；— ＝基线点不足 3 个；* ＝点数少于 5 个")
    print("  %s" % out)

    # 明细表
    path = os.path.join(SEG, "时程趋势.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["组", "被试", "指标", "单位", "有效点数", "基线段数",
                    "斜率_全部(原始/次)", "斜率_全部(标准差/次)", "r_全部",
                    "斜率_基线(原始/次)", "斜率_基线(标准差/次)", "r_基线"])
        for group in GROUPS:
            for subject in SUBJECTS:
                for col, _t, _u in FEATURES:
                    rec = records.get((group, subject, col))
                    if not rec:
                        continue
                    w.writerow([group, subject, rec["title"], rec["unit"],
                                rec["n_all"], rec["n_base"],
                                "" if rec["slope"] is None else round(rec["slope"], 4),
                                "" if rec["slope_z"] is None else round(rec["slope_z"], 4),
                                "" if rec["r"] is None else round(rec["r"], 3),
                                "" if rec["b_slope"] is None else round(rec["b_slope"], 4),
                                "" if rec["b_slope_z"] is None else round(rec["b_slope_z"], 4),
                                "" if rec["b_r"] is None else round(rec["b_r"], 3)])
    print("  %s" % path)

    # 控制台摘要：心率与 HRV 的基线漂移
    print("\n基线任务随任次的漂移（标准差/次，正=越飞越高）:")
    keys = [("hr_mean", "平均心率"), ("sdnn", "SDNN"), ("rmssd", "RMSSD"),
            ("scr_rate", "皮电反应频率")]
    print("  %-6s %-8s %s" % ("组", "指标", "  ".join("%8s" % s for s in SUBJECTS)))
    for group in GROUPS:
        for col, title in keys:
            cells = []
            for subject in SUBJECTS:
                rec = records.get((group, subject, col))
                if not rec:
                    cells.append("       —")
                elif rec["slope_z"] is None:
                    cells.append("       —")
                else:
                    cells.append("%+8.2f" % rec["slope_z"])
            print("  %-6s %-8s %s" % (group, title, "  ".join(cells)))


if __name__ == "__main__":
    main()
