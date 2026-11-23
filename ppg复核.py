# -*- coding: utf-8 -*-
"""按「序号×1/fs」重建时间轴后，逐场检查 PPG 主频是否与心电一致。

关键背景：手环是整包落库，一包 13~14 个样本共用同一个时间戳（1 分钟 15030 行
只有 1098 个不同时间戳）。若用这些时间戳插值重建时间轴，等效采样率会从 250 Hz
塌到 ~18 Hz，频谱完全失真——早期「PPG 没有脉搏波」的判断就是这么来的。
"""
import glob
import json
import os
import sqlite3
from datetime import datetime

import numpy as np
from scipy import signal as sg

from ppg_pipeline.data_loader import FS_ECG, FS_PPG

APP = r"C:\Users\lenovo\Desktop\试飞院二期\AAAwindows_code"
COLS = ("PPG1", "PPG2", "PPG3", "PPG4")


def epoch(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def window(con, table, t0, sec, cols=None):
    a = datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S")
    b = datetime.fromtimestamp(t0 + sec).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute('SELECT value FROM "%s" WHERE time>=? AND time<? ORDER BY rowid'
                       % table, (a, b)).fetchall()
    if len(rows) < 100:
        return None
    if cols:
        return np.array([[json.loads(r[0])[c] for c in cols] for r in rows], float)
    return np.array([float(r[0]) for r in rows], float)


def ecg_hr(con, t0, sec):
    e = window(con, "ecg_data", t0, sec)
    if e is None or len(e) < 500:
        return None
    fs = FS_ECG
    y = sg.detrend(e)
    hi = min(40, fs * 0.45)
    b, a = sg.butter(2, [3 / (fs / 2), hi / (fs / 2)], btype="band")
    f = sg.filtfilt(b, a, y)
    pk, _ = sg.find_peaks(f, prominence=1.0 * f.std(), distance=int(fs * 0.33))
    if len(pk) < 6:
        return None
    rr = np.diff(pk) / fs
    rr = rr[(rr > 0.55 * np.median(rr)) & (rr < 1.6 * np.median(rr))]
    if len(rr) < 5 or rr.std() / rr.mean() > 0.25:
        return None
    return float(60 / np.median(rr))


def ppg_peaks(A, sec):
    n, fs = A.shape[0], FS_PPG
    out = []
    for i in range(A.shape[1]):
        y = sg.detrend(A[:, i])
        try:
            f = sg.filtfilt(*sg.butter(3, [0.7 / (fs / 2), 5 / (fs / 2)], btype="band"), y)
        except ValueError:
            out.append(None)
            continue
        fr, p = sg.welch(f, fs=fs, nperseg=int(fs * 30))
        band = (fr >= 0.7) & (fr <= 3.0)
        if not band.any():
            out.append(None)
            continue
        k = int(np.argmax(p[band]))
        out.append((60 * fr[band][k], p[band][k] / max(np.median(p[band]), 1e-12)))
    return out


def main():
    dbs = [p for p in sorted(glob.glob(os.path.join(APP, "data", "**", "*.db"), recursive=True))
           if os.path.getsize(p) > 1048576]
    print("%-16s %6s | %s" % ("会话", "ECG", "各窗口四通道主频(bpm)；✓=与心电差<8bpm"))
    total = match = 0
    for p in dbs:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "bracelet_ppg_data" not in tabs:
            con.close()
            continue
        name = os.path.basename(os.path.dirname(os.path.dirname(p)))
        mn, mx = con.execute("SELECT MIN(time), MAX(time) FROM bracelet_ppg_data").fetchone()
        if not mn:
            con.close()
            continue
        a, b = epoch(mn), epoch(mx)
        lines = []
        for frac in (0.2, 0.4, 0.6, 0.8):
            t0 = a + (b - a - 60) * frac
            hr = ecg_hr(con, t0, 60)
            A = window(con, "bracelet_ppg_data", t0, 60, COLS)
            if A is None:
                continue
            peaks = ppg_peaks(A, 60)
            txt = []
            for pk in peaks:
                if pk is None:
                    txt.append("  —  ")
                else:
                    mark = ""
                    if hr:
                        total += 1
                        if abs(pk[0] - hr) < 8:
                            match += 1
                            mark = "✓"
                    txt.append("%3.0f%s " % (pk[0], mark))
            lines.append("[%s|ECG %s] %s" % (
                "%.0f%%" % (frac * 100), "%.0f" % hr if hr else "—", "".join(txt)))
        con.close()
        print("%-16s\n    %s" % (name, "\n    ".join(lines)))
    if total:
        print("\n四通道共 %d 个判定，其中与心电相差 <8 bpm 的 %d 个（%.0f%%）"
              % (total, match, 100.0 * match / total))


if __name__ == "__main__":
    main()
