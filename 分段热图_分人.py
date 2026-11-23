# -*- coding: utf-8 -*-
"""按被试拆开的条件均值热图。

原 `图2_条件均值热图.png` 把三个人合并平均，个体方向相反时会互相抵消成 0
（典型例子：侧风三个人心率 -0.64 / +0.61 / -0.64）。这个脚本把合并版拆成
每人一张，同一色标（-1~1），便于横向对照。

输出（都在 实验分段/）：
    图2a_热图_晁超.png
    图2b_热图_马国庆.png
    图2c_热图_高晨瑞.png
    图2d_三人并排对比.png
"""
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SEG = os.path.join(ROOT, "实验分段")

KIND_ORDER = ["基线", "低能见度", "侧风", "复合"]
SUBJECTS = ["晁超", "马国庆", "高晨瑞"]
SUBJECT_FILES = {"晁超": "图2a_热图_晁超.png",
                 "马国庆": "图2b_热图_马国庆.png",
                 "高晨瑞": "图2c_热图_高晨瑞.png"}
# 每位被试整体的数据规模，标题里给出来，方便判断哪些格子不能当真
SUBJECT_NOTE = {
    "晁超": "心电可用 15 段 · 手环可用 16 段 · 低能见度整组心电未接通",
    "马国庆": "心电可用 21 段 · 手环可用 17 段 · 低能见度 4 段 + 复合 1 段",
    "高晨瑞": "心电可用 13 段 · 手环可用 13 段 · 每格仅 2~4 段",
}

FEATURES = [
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


def load_rows():
    path = os.path.join(SEG, "特征表.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for row in rows:
        for col, _ in FEATURES:
            for key in (col, col + "_z"):
                raw = row.get(key)
                try:
                    row[key] = float(raw) if raw not in (None, "") else None
                except ValueError:
                    row[key] = None
    return rows


def build_matrix(rows, subject):
    """返回 (z 均值矩阵, 段数矩阵)；缺数据处为 NaN。"""
    sub = [r for r in rows if r["subject"] == subject]
    z, cnt = [], []
    for col, _title in FEATURES:
        line_z, line_n = [], []
        for kind in KIND_ORDER:
            vals = [r[col + "_z"] for r in sub
                    if r["kind"] == kind and r.get(col + "_z") is not None]
            line_z.append(np.mean(vals) if vals else np.nan)
            line_n.append(len(vals))
        z.append(line_z)
        cnt.append(line_n)
    return np.array(z, dtype=float), cnt


def draw(ax, matrix, counts, title, show_ylabels=True, colorbar_ax=None):
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad("#EDEBE6")                      # 缺数据：中性灰，不用颜色误导
    masked = np.ma.masked_invalid(matrix)
    im = ax.imshow(masked, cmap=cmap, vmin=-1, vmax=1, aspect="auto")

    ax.set_xticks(range(len(KIND_ORDER)))
    ax.set_xticklabels(KIND_ORDER, fontsize=10)
    ax.set_yticks(range(len(FEATURES)))
    if show_ylabels:
        ax.set_yticklabels([t for _c, t in FEATURES], fontsize=9)
    else:
        ax.set_yticklabels([])

    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            value = matrix[i, j]
            if np.isnan(value):
                ax.text(j, i, "—", ha="center", va="center",
                        fontsize=11, color="#888780")
                continue
            ax.text(j, i, "%.2f\nn=%d" % (value, counts[i][j]),
                    ha="center", va="center", fontsize=8.5, linespacing=1.35,
                    color="white" if abs(value) > 0.6 else "#222222")

    ax.set_title(title, fontsize=11.5, pad=10)
    ax.set_xticks(np.arange(-0.5, len(KIND_ORDER), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(FEATURES), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.2)
    ax.tick_params(which="minor", length=0)
    if colorbar_ax is not None:
        plt.colorbar(im, cax=colorbar_ax, label="均值 z")
    return im


def main():
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    rows = load_rows()
    matrices = {}
    for subject in SUBJECTS:
        matrix, counts = build_matrix(rows, subject)
        matrices[subject] = (matrix, counts)

        fig, ax = plt.subplots(figsize=(8.4, 6.3))
        fig.subplots_adjust(left=0.30, right=0.86, top=0.86, bottom=0.09)
        cax = fig.add_axes([0.885, 0.16, 0.022, 0.62])
        fig.suptitle("%s · 各阶段特征均值（被试内 z 分数）" % subject,
                     fontsize=12.5, y=0.975)
        fig.text(0.30, 0.922, SUBJECT_NOTE[subject], fontsize=8.5, color="#5F5E5A")
        draw(ax, matrix, counts, "", colorbar_ax=cax)
        out = os.path.join(SEG, SUBJECT_FILES[subject])
        fig.savefig(out, dpi=150)
        plt.close(fig)
        print("  %s" % out)

    # 三人纵向堆叠：四列条件上下对齐，同列直接竖着比
    fig, axes = plt.subplots(3, 1, figsize=(9.0, 12.6), sharex=True)
    fig.subplots_adjust(left=0.245, right=0.875, top=0.925, bottom=0.055,
                        hspace=0.16)
    cax = fig.add_axes([0.888, 0.36, 0.017, 0.26])
    for idx, subject in enumerate(SUBJECTS):
        matrix, counts = matrices[subject]
        draw(axes[idx], matrix, counts, subject,
             show_ylabels=True,
             colorbar_ax=cax if idx == 2 else None)
        axes[idx].set_title(subject, fontsize=12, pad=8)
    fig.suptitle("三人分开看：同一个条件，方向经常相反 —— 合并平均会把它们抵消成 0",
                 fontsize=13)
    out = os.path.join(SEG, "图2d_三人并排对比.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print("  %s" % out)

    # 打印一份对照表，方便直接读
    print("\n平均心率 z 分数（行=条件，列=被试）:")
    print("  %-8s %10s %10s %10s" % ("条件", *SUBJECTS))
    for j, kind in enumerate(KIND_ORDER):
        cells = []
        for subject in SUBJECTS:
            matrix, counts = matrices[subject]
            value = matrix[0, j]
            cells.append("—" if np.isnan(value) else "%+.2f(n=%d)" % (value, counts[0][j]))
        print("  %-8s %10s %10s %10s" % (kind, *cells))


if __name__ == "__main__":
    main()
