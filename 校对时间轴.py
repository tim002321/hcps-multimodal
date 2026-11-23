# -*- coding: utf-8 -*-
"""核对各会话库的时间轴：额定采样率 + 包锚点重建后，漂移/丢包/断点是什么水平。

用法
    python 校对时间轴.py            # 校对 信号库\\*.db
    python 校对时间轴.py 某个.db     # 校对单个库
    python 校对时间轴.py --trials    # 校对 实验分段 下的 trial.db（分段后的库）

输出每张表一行：样本数、额定采样率、实测速率、墙钟跨度、漂移、缺失样本、断点。
「漂移」是墙钟跨度 − 样本跨度，正数表示少收了样本，负数表示设备时钟略快。
旧算法用 行数/墙钟跨度 当采样率，这个表就是用来对比它到底偏了多少。
"""
import glob
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from ppg_pipeline.data_loader import (                       # noqa: E402
    FS_ACC, FS_ECG, FS_GSR, FS_PPG, timeline_from_db)

# 表名 → 额定采样率
TABLES = [
    ("bracelet_ppg_data", FS_PPG),
    ("bracelet_accel_data", FS_ACC),
    ("ecg_data", FS_ECG),
    ("bracelet_gsr_data", FS_GSR),
]

HEAD = ("%-30s %-20s %9s %6s %8s %9s %9s %8s %7s %6s %6s"
        % ("库", "表名", "样本", "额定", "实测", "墙钟(s)", "漂移(s)",
           "残差p95", "旧fs", "缺样本", "断点"))


def scan_db(path, label):
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    tabs = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out = []
    for table, fs in TABLES:
        if table not in tabs:
            continue
        n, t_min, t_max = con.execute(
            'SELECT COUNT(*), MIN(time), MAX(time) FROM "{}"'.format(table)
        ).fetchone()
        if not n:
            continue
        old_fs = float("nan")
        if t_min and t_max and t_min != t_max:
            from datetime import datetime
            try:
                span = (datetime.fromisoformat(t_max)
                        - datetime.fromisoformat(t_min)).total_seconds()
                old_fs = n / span if span > 0 else float("nan")
            except ValueError:
                pass
        try:
            t, qc = timeline_from_db(path, table, fs)
        except Exception as exc:                              # noqa: BLE001
            print("%-30s %-20s  读取失败：%s" % (label, table, exc))
            continue
        out.append((label, table, qc, old_fs))
        print("%-30s %-20s %9d %6.1f %8.1f %9.1f %+9.3f %+8.1f %7.1f %6d %6d"
              % (label, table, qc.n, qc.fs, qc.fs_measured, qc.duration_wall,
                 qc.drift_s, qc.resid_p95_ms, old_fs, qc.lost_samples,
                 len(qc.breaks)))
        if qc.status != "ok":
            print("%-30s %-20s   [%s]" % ("", "", qc.status))
    con.close()
    return out


def main(argv):
    if "--trials" in argv:
        dbs = sorted(glob.glob(os.path.join(ROOT, "实验分段", "*", "*", "*", "trial.db")))
        base = os.path.join(ROOT, "实验分段")
    elif len(argv) > 1 and argv[1].endswith(".db"):
        dbs = [os.path.abspath(argv[1])]
        base = os.path.dirname(dbs[0])
    else:
        dbs = sorted(glob.glob(os.path.join(ROOT, "信号库", "*.db")))
        base = os.path.join(ROOT, "信号库")

    if not dbs:
        print("没找到数据库")
        return 1
    print(HEAD)
    print("-" * len(HEAD))
    rows = []
    for p in dbs:
        rows.extend(scan_db(p, os.path.relpath(p, base)))
    if not rows:
        return 1

    # 汇总：额定速率是否守住、漂移量级
    print("-" * len(HEAD))
    by_table = {}
    for _label, table, qc, old in rows:
        by_table.setdefault(table, []).append((qc, old))
    for table, items in by_table.items():
        fs_set = {round(qc.fs, 3) for qc, _ in items}
        drifts = [abs(qc.drift_s) for qc, _ in items
                  if qc.drift_s == qc.drift_s]
        olds = [old for _, old in items if old == old]
        print("%-14s 库数=%2d 额定速率=%s  |漂移| 中位=%.2fs 最大=%.2fs"
              "  旧算法 fs 范围=%.1f~%.1f"
              % (table, len(items), ",".join("%.1f" % f for f in sorted(fs_set)),
                 sorted(drifts)[len(drifts) // 2] if drifts else float("nan"),
                 max(drifts) if drifts else float("nan"),
                 min(olds) if olds else float("nan"),
                 max(olds) if olds else float("nan")))
    bad = [r for r in rows if r[2].status not in ("ok", "unsmoothed")]
    print("\n非正常状态 %d 条%s" % (len(bad), "" if not bad else "："
                                 + "、".join("%s/%s(%s)" % (b[0], b[1], b[2].status)
                                            for b in bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
