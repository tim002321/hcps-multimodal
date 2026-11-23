# -*- coding: utf-8 -*-
"""把任务时程拆成「一个指标一张图」，并按测量类别归档。

每张图内部是三行子图（第一组 / 第二组 / 第三组），同一指标在三个子图里
共用 y 轴范围，因此三组可以竖着直接比。点形状表示条件（○基线 / △特殊），
虚线是该被试全部点的最小二乘趋势。

输出：图集/折线图_单指标/<类别>/<序号>_<指标名>.png
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
OUT = os.path.join(ROOT, "图集", "折线图_单指标")
OUT_SPLIT = os.path.join(ROOT, "图集", "折线图_按组拆分")

GROUPS = ["第一组", "第二组", "第三组"]
SUBJECTS = ["晁超", "马国庆", "高晨瑞"]
SUBJECT_COLORS = {"晁超": "#c0504d", "马国庆": "#4f81bd", "高晨瑞": "#9bbb59"}

# (文件夹, [(列名, 显示名, 单位), ...])
CATEGORIES = [
    ("01_心率", [
        ("hr_mean", "平均心率", "bpm"),
        ("hr_sd", "心率标准差", "bpm"),
        ("hr_min", "心率P5", "bpm"),
        ("hr_max", "心率P95", "bpm"),
    ]),
    ("02_心率变异性", [
        ("sdnn", "SDNN", "ms"),
        ("rmssd", "RMSSD", "ms"),
        ("pnn50", "pNN50", "%"),
        ("lf_hf", "LF_HF", "比值"),
    ]),
    ("03_段内心率时程", [
        ("hr_slope", "段内心率斜率", "bpm/min"),
        ("hr_drift", "段内前后心率差", "bpm"),
    ]),
    ("04_活动与加速度", [
        ("acc_mean", "加速度均值", "g"),
        ("acc_sd", "加速度标准差", "g"),
        ("acc_active", "活动占比", "%"),
    ]),
    ("05_皮电EDA", [
        ("scl_mean", "皮电SCL均值", ""),
        ("scl_range", "皮电SCL极差", ""),
        ("scr_count", "皮电反应次数", "次"),
        ("scr_rate", "皮电反应频率", "次/min"),
    ]),
    ("06_体温", [
        ("temp_mean", "体温", "℃"),
        ("temp_range", "体温极差", "℃"),
    ]),
]

ALL_FEATURES = [(c, t, u) for _d, items in CATEGORIES for (c, t, u) in items]


def load_rows():
    rows = list(csv.DictReader(open(os.path.join(SEG, "特征表.csv"),
                                    encoding="utf-8-sig")))
    for row in rows:
        row["task"] = int(row["task"])
        for col, _t, _u in ALL_FEATURES:
            raw = row.get(col)
            try:
                row[col] = float(raw) if raw not in (None, "") else np.nan
            except ValueError:
                row[col] = np.nan
    return rows


def series_of(rows, subject, group, col):
    sub = sorted([r for r in rows if r["subject"] == subject and r["grp"] == group],
                 key=lambda r: r["task"])
    x = np.array([r["task"] for r in sub], dtype=float)
    y = np.array([r[col] for r in sub], dtype=float)
    base = np.array([r["kind"] == "基线" for r in sub])
    return x, y, base


def linear_fit(x, y):
    m = np.isfinite(y)
    if m.sum() < 3:
        return None
    xs, ys = x[m], y[m]
    if np.ptp(xs) == 0 or np.ptp(ys) == 0:
        return None
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def compute_ylim(rows, col):
    """该指标跨三组的统一 y 范围，保证 57 张单组图之间也能直接比。"""
    vals = np.concatenate([
        np.array([r[col] for r in rows if r["grp"] == g], dtype=float)
        for g in GROUPS])
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None
    lo, hi = float(vals.min()), float(vals.max())
    pad = (hi - lo) * 0.12 if hi > lo else max(abs(hi) * 0.1, 1.0)
    return (lo - pad, hi + pad)


def draw_single_group(rows, col, title, unit, group, ylim, out_path):
    """一个指标 × 一个组，单独一张图。图例里带该被试的趋势斜率。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(9.4, 5.4))
    fig.subplots_adjust(left=0.10, right=0.975, top=0.80, bottom=0.20)

    present, n_txt = [], []
    for subject in SUBJECTS:
        x, y, base = series_of(rows, subject, group, col)
        n_seg = int(np.isfinite(y).sum())
        if n_seg == 0:
            continue
        present.append(subject)
        n_txt.append("%s %d 段" % (subject, n_seg))
        color = SUBJECT_COLORS[subject]
        ax.plot(x, y, "-", color=color, lw=1.9, alpha=0.92, zorder=2)
        ax.plot(x[base], y[base], "o", color=color, ms=7.0,
                mec="white", mew=0.9, zorder=3)
        ax.plot(x[~base], y[~base], "^", color=color, ms=8.0,
                mec="white", mew=0.9, zorder=3)
        fit = linear_fit(x, y)
        if fit:
            slope, intercept = fit
            xs = np.linspace(np.nanmin(x), np.nanmax(x), 2)
            ax.plot(xs, slope * xs + intercept, "--", color=color,
                    lw=1.3, alpha=0.72, zorder=1)

    ax.set_ylim(*ylim)
    ax.set_xlim(0.5, 10.5)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(alpha=0.3, lw=0.7)
    ax.tick_params(labelsize=10)
    ax.set_xlabel("组内任务序号", fontsize=10.5)
    ax.set_ylabel("%s%s" % (title, "（%s）" % unit if unit else ""), fontsize=10.5)

    # 图例：颜色 + 该被试的趋势斜率
    handles = []
    for subject in present:
        x, y, _b = series_of(rows, subject, group, col)
        fit = linear_fit(x, y)
        tail = "%+.2f/次" % fit[0] if fit else "—"
        handles.append(Line2D([], [], color=SUBJECT_COLORS[subject], lw=2.0,
                              marker="o", ms=7, mec="white",
                              label="%s  %s" % (subject, tail)))
    handles += [Line2D([], [], color="#5F5E5A", lw=0, marker="o", ms=7,
                       mec="white", label="基线任务"),
                Line2D([], [], color="#5F5E5A", lw=0, marker="^", ms=8,
                       mec="white", label="特殊条件"),
                Line2D([], [], color="#5F5E5A", lw=1.3, ls="--", label="趋势")]
    ax.legend(handles=handles, loc="lower center", ncol=len(handles),
              frameon=False, fontsize=9.2, bbox_to_anchor=(0.5, 1.005),
              handletextpad=0.5, columnspacing=1.5)

    fig.suptitle("%s%s　·　%s" % (title, "（%s）" % unit if unit else "", group),
                 fontsize=15, y=0.975)
    total = len([r for r in rows if r["grp"] == group])
    fig.text(0.5, 0.898, "本组共 %d 段（有效：%s）" % (total, "　".join(n_txt)),
             ha="center", fontsize=9.5, color="#5F5E5A")
    fig.text(0.5, 0.022,
             "图例里人名后面的数字＝该被试的趋势斜率（%s/次），只指示方向，不做统计推断　·　"
             "本图 y 轴与同指标的其他组一致，可直接比较"
             % (unit if unit else "原始单位"),
             ha="center", fontsize=8.5, color="#5F5E5A")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def draw_feature(rows, col, title, unit, out_path):
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    # 先扫一遍该指标在所有组的值，定统一 y 范围（三组可比）
    vals = np.concatenate([
        np.array([r[col] for r in rows if r["grp"] == g], dtype=float)
        for g in GROUPS])
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None
    lo, hi = float(vals.min()), float(vals.max())
    pad = (hi - lo) * 0.12 if hi > lo else max(abs(hi) * 0.1, 1.0)
    ylim = (lo - pad, hi + pad)

    fig, axes = plt.subplots(3, 1, figsize=(9.4, 10.2))
    for k, group in enumerate(GROUPS):
        ax = axes[k]
        present = []
        for subject in SUBJECTS:
            x, y, base = series_of(rows, subject, group, col)
            if not np.isfinite(y).any():
                continue
            present.append(subject)
            color = SUBJECT_COLORS[subject]
            ax.plot(x, y, "-", color=color, lw=1.6, alpha=0.9, zorder=2)
            ax.plot(x[base], y[base], "o", color=color, ms=5.2,
                    mec="white", mew=0.7, zorder=3)
            ax.plot(x[~base], y[~base], "^", color=color, ms=6.0,
                    mec="white", mew=0.7, zorder=3)
            fit = linear_fit(x, y)
            if fit:
                slope, intercept = fit
                xs = np.linspace(np.nanmin(x), np.nanmax(x), 2)
                ax.plot(xs, slope * xs + intercept, "--", color=color,
                        lw=1.1, alpha=0.7, zorder=1)

        ax.set_ylim(*ylim)
        ax.set_xlim(0.5, 10.5)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(alpha=0.28, lw=0.6)
        ax.tick_params(labelsize=9)
        ax.set_ylabel("%s%s" % (title, "（%s）" % unit if unit else ""), fontsize=9.5)
        n_seg = len([r for r in rows if r["grp"] == group])
        note = "　".join(
            "%s %d 段" % (s, len([r for r in rows
                                 if r["subject"] == s and r["grp"] == group]))
            for s in present)
        ax.set_title("%s（共 %d 段：%s）" % (group, n_seg, note),
                     loc="left", fontsize=10.5, pad=6)
        if k == len(GROUPS) - 1:
            ax.set_xlabel("组内任务序号", fontsize=10)

    handles = [Line2D([], [], color=SUBJECT_COLORS[s], lw=1.8, marker="o", ms=5,
                      mec="white", label=s) for s in SUBJECTS]
    handles += [Line2D([], [], color="#5F5E5A", lw=0, marker="o", ms=5.5,
                       mec="white", label="基线任务"),
                Line2D([], [], color="#5F5E5A", lw=0, marker="^", ms=6.5,
                       mec="white", label="特殊条件"),
                Line2D([], [], color="#5F5E5A", lw=1.1, ls="--", label="趋势")]
    fig.suptitle("%s%s　随任务推进的变化" % (title, "（%s）" % unit if unit else ""),
                 fontsize=14, y=0.985)
    fig.legend(handles=handles, loc="upper center", ncol=len(handles),
               frameon=False, fontsize=9.5, bbox_to_anchor=(0.5, 0.966))
    # 说明放图底部当脚注，避免和图例抢空间
    fig.text(0.5, 0.012,
             "横轴＝组内第几次飞行　·　三行共用 y 轴，可上下对比　·　"
             "第三组点数少（晁超 5 / 马国庆 5 但心电仅 1 / 高晨瑞 3），仅示意",
             ha="center", fontsize=8.5, color="#5F5E5A")
    fig.tight_layout(rect=[0, 0.028, 1, 0.945])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def write_split_readme(rows_index):
    md = ["# 单指标 × 单组 折线图", "",
          "一个指标配一个组，各出一张，共 %d 张。同一指标的三张图 **y 轴范围相同**，" % len(rows_index),
          "所以横向翻看时幅度可以直接比。",
          "",
          "- 点形状：○ 基线任务，△ 特殊条件任务（低能见度 / 侧风 / 复合）",
          "- 图例里人名后面跟的是**该被试的趋势斜率**（原始单位/次），只指示方向，`—` 表示点太少算不出",
          "- 虚线是该被试全部点的最小二乘，不做统计推断",
          "- 第三组点数少（晁超 5 段、马国庆 5 段但心电仅 1 段、高晨瑞 3 段），仅作示意",
          "",
          "| 类别 | 指标 | 单位 | 组 | 文件 |",
          "|---|---|---|---|---|"]
    for folder, title, unit, fname, _c, group in rows_index:
        md.append("| %s | %s | %s | %s | `%s/%s` |"
                  % (folder, title, unit or "—", group, folder, fname))
    md += ["", "重新生成：`python 时程分图.py --split-group`（约 40 秒）", ""]
    with open(os.path.join(OUT_SPLIT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))


def main():
    import argparse

    parser = argparse.ArgumentParser(description="单指标时程折线图")
    parser.add_argument("--split-group", action="store_true",
                        help="额外输出「一个指标 × 一个组」的拆分版（57 张）")
    parser.add_argument("--only-split", action="store_true",
                        help="只出拆分版，跳过 19 张合并版")
    args = parser.parse_args()

    rows = load_rows()
    index = []
    if not args.only_split:
        for folder, items in CATEGORIES:
            print("[合并版 %s]" % folder)
            for n, (col, title, unit) in enumerate(items, 1):
                safe = title.replace("/", "_")
                path = os.path.join(OUT, folder, "%02d_%s.png" % (n, safe))
                got = draw_feature(rows, col, title, unit, path)
                if got:
                    print("   %s" % os.path.basename(got))
                    index.append((folder, title, unit, "%02d_%s.png" % (n, safe), col))

    if args.split_group or args.only_split:
        split_rows = []
        for folder, items in CATEGORIES:
            print("[拆分版 %s]" % folder)
            for n, (col, title, unit) in enumerate(items, 1):
                ylim = compute_ylim(rows, col)
                if ylim is None:
                    continue
                safe = title.replace("/", "_")
                for group in GROUPS:
                    # 中文按 Unicode 码点排序会变成 一→三→二，加 G 序号前缀固定顺序
                    gidx = GROUPS.index(group) + 1
                    fname = "%02d_%s_G%d%s.png" % (n, safe, gidx, group)
                    path = os.path.join(OUT_SPLIT, folder, fname)
                    got = draw_single_group(rows, col, title, unit, group,
                                            ylim, path)
                    if got:
                        print("   %s" % fname)
                        split_rows.append((folder, title, unit, fname, col, group))
        write_split_readme(split_rows)

    # 索引
    md = ["# 单指标折线图", "",
          "一项测量一张图。每张图内三行分别是第一组 / 第二组 / 第三组，",
          "**三行共用 y 轴范围**，所以可以直接上下比较幅度。",
          "",
          "- 点形状：○ 基线任务，△ 特殊条件任务（低能见度 / 侧风 / 复合）",
          "- 虚线：该被试全部点的最小二乘趋势，只指示方向，不做统计推断",
          "- 第三组点数少（晁超 5 段、马国庆 5 段但心电仅 1 段、高晨瑞 3 段），仅作示意",
          "",
          "| 类别 | 指标 | 单位 | 文件 |",
          "|---|---|---|---|"]
    for folder, title, unit, fname, _c in index:
        md.append("| %s | %s | %s | `%s/%s` |"
                  % (folder, title, unit or "—", folder, fname))
    md += ["", "重新生成：`python 时程分图.py`（约 15 秒）", ""]
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("\n共 %d 张，输出目录：%s" % (len(index), OUT))


if __name__ == "__main__":
    main()
