"""从 实验分段.db 汇总统计：按 被试 × 实验条件 出描述统计与对比图。

输出
    实验分段\\条件汇总.csv     一次（被试×条件）一行
    实验分段\\条件对比.png     心率 / HRV / 活动量的条件对比

用法
    python 分段统计.py
"""

import csv
import os
import sqlite3

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SEG = os.path.join(ROOT, "实验分段")
DB = os.path.join(SEG, "实验分段.db")

KIND_ORDER = ["基线", "低能见度", "侧风", "复合"]   # 单发失效已并入复合
METRICS = ["hr_mean", "hr_sd", "sdnn", "rmssd", "lf_hf", "acc_active", "scl_mean"]


def load_wide():
    con = sqlite3.connect(DB)
    trials = {}
    for row in con.execute("""select id,subject,grp,task,kind,start,end,duration_min,
                                     setting,note,vis_m,wind_dir,wind_speed,failure,
                                     failure_phase from trials"""):
        trials[row[0]] = dict(id=row[0], subject=row[1], grp=row[2], task=row[3],
                              kind=row[4], start=row[5], end=row[6], dur=row[7],
                              setting=row[8] or "", note=row[9] or "",
                              vis=row[10], wind=row[11], wspeed=row[12],
                              failure=row[13], phase=row[14])
    for tid, metric, value in con.execute(
            "select trial_id, metric, value from trial_features"):
        if tid in trials and metric in METRICS:
            trials[tid][metric] = value
    qc = {}
    for tid, ch, status in con.execute("select trial_id, channel, status from trial_qc"):
        qc[(tid, ch)] = status
    con.close()
    for tid, t in trials.items():
        t["ecg_ok"] = qc.get((tid, "ECG")) == "ok"
        t["gsr_ok"] = qc.get((tid, "GSR")) == "ok"
    return list(trials.values())


def agg(rows, key_metrics):
    out = {}
    for m in key_metrics:
        v = [r[m] for r in rows if r.get(m) is not None]
        out[m] = (float(np.mean(v)), float(np.std(v, ddof=1)) if len(v) > 1 else 0.0, len(v)) if v else (None, None, 0)
    return out


def main():
    rows = load_wide()
    print("任务段 %d，心电可用 %d" % (len(rows), sum(1 for r in rows if r["ecg_ok"])))

    # ---- 一次（被试×条件）一行
    keys = []
    for r in rows:
        k = (r["subject"], r["kind"])
        if k not in keys:
            keys.append(k)
    keys.sort(key=lambda k: (k[0], KIND_ORDER.index(k[1]) if k[1] in KIND_ORDER else 9))

    lines = []
    for subject, kind in keys:
        sub = [r for r in rows if r["subject"] == subject and r["kind"] == kind]
        a = agg(sub, METRICS)
        fails = sum(1 for r in sub if "降落失败" in r["note"] or "复飞" in r["note"])
        lines.append(dict(
            姓名=subject, 条件类型=kind, 任务数=len(sub),
            心电可用=sum(1 for r in sub if r["ecg_ok"]),
            平均心率=round(a["hr_mean"][0], 1) if a["hr_mean"][0] else "",
            心率SD=round(a["hr_mean"][1], 1) if a["hr_mean"][0] else "",
            SDNN=round(a["sdnn"][0], 1) if a["sdnn"][0] else "",
            RMSSD=round(a["rmssd"][0], 1) if a["rmssd"][0] else "",
            LFHF=round(a["lf_hf"][0], 2) if a["lf_hf"][0] else "",
            活动占比=round(a["acc_active"][0], 3) if a["acc_active"][0] else "",
            SCL=round(a["scl_mean"][0], 3) if a["scl_mean"][0] else "",
            平均时长min=round(float(np.mean([r["dur"] for r in sub])), 1),
            降落失败数=fails,
            备注=""))

    out_csv = os.path.join(SEG, "条件汇总.csv")
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(lines[0].keys()))
        w.writeheader()
        w.writerows(lines)
    print("写出", out_csv)

    # ---- 全被试合计（条件层面）
    print("\n条件层面（全部被试合计，仅心电可用段）：")
    for kind in KIND_ORDER:
        sub = [r for r in rows if r["kind"] == kind and r["ecg_ok"]]
        if not sub:
            continue
        hr = np.array([r["hr_mean"] for r in sub])
        sdnn = np.array([r["sdnn"] for r in sub if r.get("sdnn")])
        print("  %-6s n=%2d  心率 %.1f±%.1f   SDNN %s" % (
            kind, len(sub), hr.mean(), hr.std(ddof=1),
            "%.1f±%.1f" % (sdnn.mean(), sdnn.std(ddof=1)) if len(sdnn) > 1 else "—"))

    # ---- 画图
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        subjects = sorted({r["subject"] for r in rows})
        colors = {"晁超": "#c0504d", "马国庆": "#4f81bd", "高晨瑞": "#9bbb59"}
        fig, axes = plt.subplots(1, 2, figsize=(13, 5))
        for ax, metric, title in ((axes[0], "hr_mean", "平均心率 (bpm)"),
                                  (axes[1], "rmssd", "RMSSD (ms)")):
            for si, s in enumerate(subjects):
                for ki, kind in enumerate(KIND_ORDER):
                    sub = [r[metric] for r in rows
                           if r["subject"] == s and r["kind"] == kind
                           and r["ecg_ok"] and r.get(metric) is not None]
                    if not sub:
                        continue
                    jitter = (np.random.RandomState(si * 10 + ki).rand(len(sub)) - 0.5) * 0.18
                    ax.scatter(ki + (si - 1) * 0.22 + jitter, sub, s=26, alpha=0.75,
                               color=colors.get(s, "gray"), label=s if ki == 0 else None)
                    ax.plot([ki + (si - 1) * 0.22 - 0.08, ki + (si - 1) * 0.22 + 0.08],
                            [np.mean(sub)] * 2, color=colors.get(s, "gray"), lw=2.2)
            ax.set_xticks(range(len(KIND_ORDER)))
            ax.set_xticklabels(KIND_ORDER)
            ax.set_title(title)
            ax.grid(alpha=0.25, axis="y")
        axes[0].legend(fontsize=8, frameon=False)
        fig.suptitle("按实验条件分段后的指标分布（点=单次飞行，横线=该被试均值）", fontsize=11)
        fig.tight_layout(rect=[0, 0, 1, 0.95])
        png = os.path.join(SEG, "条件对比.png")
        fig.savefig(png, dpi=130)
        print("写出", png)
    except Exception as exc:
        print("画图跳过：", exc)


if __name__ == "__main__":
    main()
