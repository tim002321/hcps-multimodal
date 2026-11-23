# -*- coding: utf-8 -*-
"""四通道 PPG 是否有脉搏波：频谱 + 自相关 + 与心电心率对照。"""
import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta

import numpy as np
from scipy import signal as sg

from ppg_pipeline.data_loader import FS_ECG, FS_PPG

APP = r"C:\Users\lenovo\Desktop\试飞院二期\AAAwindows_code"


def epoch(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def load(con, table, t0, t1, cols=None):
    rows = con.execute('SELECT time, value FROM "%s" WHERE time>=? AND time<? ORDER BY time'
                       % table, (t0, t1)).fetchall()
    if len(rows) < 50:
        return None, None
    ts = np.array([epoch(r[0]) for r in rows])
    if cols:
        vals = np.array([[json.loads(r[1])[c] for c in cols] for r in rows], dtype=float)
    else:
        vals = np.array([float(r[1]) for r in rows], dtype=float)
    return ts, vals


def uniform(ts, x, fs):
    grid = np.arange(ts[0], ts[-1], 1.0 / fs)
    return np.interp(grid, ts, x)


def ecg_hr(con, t0, t1):
    ts, e = load(con, "ecg_data", t0, t1)
    if ts is None:
        return None
    fs = FS_ECG
    y = sg.detrend(e)
    hi = min(40, fs * 0.45)
    b, a = sg.butter(2, [3 / (fs / 2), hi / (fs / 2)], btype="band")
    f = sg.filtfilt(b, a, y)
    if np.percentile(f, 99) - np.percentile(f, 1) < 100:
        return None
    pk, _ = sg.find_peaks(f, prominence=1.0 * f.std(), distance=int(fs * 0.33))
    if len(pk) < 6:
        return None
    rr = np.diff(pk) / fs
    keep = (rr > 0.55 * np.median(rr)) & (rr < 1.6 * np.median(rr))
    rr = rr[keep]
    if len(rr) < 5 or rr.std() / rr.mean() > 0.25:
        return None
    return float(60 / np.median(rr))


def ppg_channels(con, t0, t1):
    cols = ("PPG1", "PPG2", "PPG3", "PPG4")
    ts, A = load(con, "bracelet_ppg_data", t0, t1, cols)
    if ts is None:
        return None
    fs = FS_PPG
    res = []
    for i in range(4):
        # 手环按 250 sps 等间隔采样，行序号即时间；不能拿整包时间戳去插值
        # （一包十几个点共用一个时间戳，插值会把波形压成阶梯、采样率塌到十几 Hz）
        y = A[:, i]
        # 直流占比：AC/DC
        dc = abs(np.mean(y))
        ac = np.percentile(y, 99) - np.percentile(y, 1)
        y = sg.detrend(y)
        b, a = sg.butter(3, [0.6 / (fs / 2), 4.0 / (fs / 2)], btype="band")
        f = sg.filtfilt(b, a, y)
        fr, p = sg.welch(f, fs=fs, nperseg=min(int(fs * 20), len(f)))
        m = (fr >= 0.7) & (fr <= 3.5)
        if not m.any():
            res.append(None)
            continue
        k = int(np.argmax(p[m]))
        fpk = fr[m][k]
        prom = p[m][k] / max(np.median(p[m]), 1e-12)
        acf = np.correlate(f, f, "full")[len(f) - 1:]
        acf = acf / (acf[0] if acf[0] else 1)
        lo, hi = int(fs / 3.5), min(int(fs / 0.7), len(acf) - 1)
        kac = int(np.argmax(acf[lo:hi])) + lo if hi > lo else 0
        res.append(dict(hr_spec=60 * fpk, prom=prom,
                        hr_ac=60 * fs / kac if kac else np.nan,
                        ac=acf[kac] if kac else np.nan,
                        acdc=ac / dc if dc else np.nan))
    return fs, res


def main():
    dbs = [p for p in sorted(glob.glob(os.path.join(APP, "data", "**", "*.db"), recursive=True))
           if os.path.getsize(p) > 1048576]
    print("库数 %d" % len(dbs))
    print("%-16s %6s %6s | %s" % ("会话", "fs", "ECG", "PPG1 / PPG2 / PPG3 / PPG4  谱峰HR(突出度)"))
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
        t_mn, t_mx = datetime.fromisoformat(mn), datetime.fromisoformat(mx)
        start = t_mn + (t_mx - t_mn) * 0.3
        win = (start.strftime("%Y-%m-%d %H:%M:%S"),
               (start + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S"))
        hr_e = ecg_hr(con, *win)
        got = ppg_channels(con, *win)
        con.close()
        if not got:
            print("%-16s   无数据" % name)
            continue
        fs, chs = got
        parts = []
        for c in chs:
            if c is None:
                parts.append("   —   ")
            else:
                parts.append("%5.0f(%4.1f)" % (c["hr_spec"], c["prom"]))
        print("%-16s %6.1f %6s | %s" % (name, fs, "%.0f" % hr_e if hr_e else "—",
                                        " ".join(parts)))


if __name__ == "__main__":
    main()
