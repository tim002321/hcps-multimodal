# -*- coding: utf-8 -*-
"""画滤波后的 PPG 四通道 + 心电，肉眼判断有没有脉搏波。"""
import json
import os
import sqlite3
from datetime import datetime

import numpy as np
import cv2
from scipy import signal as sg

from ppg_pipeline.data_loader import FS_ECG, FS_PPG

APP = r"C:\Users\lenovo\Desktop\试飞院二期\AAAwindows_code"
CASES = [
    ("晁超_单发失效（谱峰 81 vs 心电 83）",
     r"data\晁超_单发失效\1_20261105_181221\20261105_181245.db", "2026-11-05 18:30:00"),
    ("高晨瑞_复合（谱峰 42~51 vs 心电 77）",
     r"data\高晨瑞_复合\1_20261106_185031\20261106_185039.db", "2026-11-06 19:00:00"),
]


def epoch(t):
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").timestamp()


def grab(con, table, t0, sec, cols=None):
    t1 = datetime.fromtimestamp(t0 + sec).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute('SELECT time,value FROM "%s" WHERE time>=? AND time<? ORDER BY time' % table,
                       (datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S"), t1)).fetchall()
    ts = np.array([epoch(r[0]) for r in rows])
    if cols:
        v = np.array([[json.loads(r[1])[c] for c in cols] for r in rows], dtype=float)
    else:
        v = np.array([float(r[1]) for r in rows], dtype=float)
    return ts, v


def panel(con, t0, sec=20):
    cols = ("PPG1", "PPG2", "PPG3", "PPG4")
    ts, A = grab(con, "bracelet_ppg_data", t0, sec, cols)
    if len(ts) < 50:
        return None
    fs = FS_PPG
    rows = []
    for i in range(4):
        # 手环按 250 sps 等间隔采样，行序号即时间，直接取原序列；
        # 不能按整包时间戳插值（一包十几个点共用一个时间戳，会把波形压成阶梯）
        y = A[:, i]
        dc = abs(np.mean(y))
        ac = np.percentile(y, 99) - np.percentile(y, 1)
        d = sg.detrend(y)
        b, a = sg.butter(3, [0.7 / (fs / 2), 5.0 / (fs / 2)], btype="band")
        f = sg.filtfilt(b, a, d)
        rows.append((f, ac / dc if dc else 0, fs))
    te, e = grab(con, "ecg_data", t0, sec)
    ecg = None
    if len(te) > 100:
        fse = FS_ECG
        y = sg.detrend(e)
        b, a = sg.butter(2, [2.0 / (fse / 2), min(40, fse * 0.45) / (fse / 2)], btype="band")
        ecg = sg.filtfilt(b, a, y)
    return rows, ecg


def draw(tiles, label):
    """每行一条曲线，统一成 900x110。"""
    out = []
    for name, sig, color in tiles:
        h, w = 110, 900
        img = np.full((h, w, 3), 255, np.uint8)
        if sig is not None and len(sig) > 10:
            s = sig[:w] if len(sig) > w else np.interp(np.linspace(0, len(sig) - 1, w),
                                                       np.arange(len(sig)), sig)
            lo, hi = np.percentile(s, 2), np.percentile(s, 98)
            if hi > lo:
                s = (s - lo) / (hi - lo)
                s = np.clip(s, 0, 1)
                pts = np.stack([np.arange(w), (h - 20 - s * (h - 40)).astype(int)], 1)
                cv2.polylines(img, [pts], False, color, 1, cv2.LINE_AA)
        cv2.putText(img, name, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 30, 30), 1)
        cv2.line(img, (0, h - 1), (w, h - 1), (230, 230, 230), 1)
        out.append(img)
    head = np.full((26, 900, 3), 255, np.uint8)
    cv2.putText(head, label, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 160), 1)
    return np.vstack([head] + out)


def main():
    blocks = []
    for label, rel, t0 in CASES:
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(APP, rel).replace("\\", "/"), uri=True)
        got = panel(con, epoch(t0))
        con.close()
        if not got:
            continue
        rows, ecg = got
        tiles = []
        for i, (f, acdc, fs) in enumerate(rows):
            tiles.append(("PPG%d  0.7-5Hz  AC/DC=%.2f%%" % (i + 1, 100 * acdc), f, (60, 90, 200)))
        if ecg is not None:
            tiles.append(("ECG  2-40Hz", ecg, (60, 140, 60)))
        blocks.append(draw(tiles, label + "   20 秒"))
    if blocks:
        w = max(b.shape[1] for b in blocks)
        blocks = [np.hstack([b, np.full((b.shape[0], w - b.shape[1], 3), 255, np.uint8)])
                  for b in blocks]
        img = np.vstack(blocks)
        ok, buf = cv2.imencode(".png", img)
        open(r"C:\Users\lenovo\Desktop\试飞院二期\.workbuddy\tmp\ppg_wave.png", "wb").write(buf.tobytes())
        print("saved", img.shape)


if __name__ == "__main__":
    main()
