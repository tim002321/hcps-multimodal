# -*- coding: utf-8 -*-
"""
数据加载模块：从会话库 SQLite 读取四通道 PPG、三轴加速度与 ECG 参考。

数据库表结构统一为 (time TEXT, value TEXT)，value 为 JSON(PPG/ACC) 或标量(ECG)。

**时间轴不再用 n/跨度 估采样率**（旧做法）：
写入端 `core/db_storage.py::insert_many` 让一个蓝牙包的全部采样点共用一个时间戳
（手环 PPG 每包 10 点、心电每包 80 点、加速度每包 1 点），时间戳数量远少于行数。
一旦出现停顿、丢包、断连重连，`行数/墙钟跨度` 就会严重偏离真实采样率
（实测某 12 min 库算出 250.8 Hz，另一批库甚至压到 18 Hz），滤波带、峰间距、
心率全都跟着错。

现在的做法：
  1. 采样率取**额定值**（PPG 250 / ECG 250 / ACC 25，见下方常量），由协议决定，不由数据猜；
  2. 信号坐标轴仍是等间隔 `i / fs`，不做任何局部拉伸，波形与 HRV 不受到达抖动影响；
  3. 用**每个时间戳（=一个包）的到达时刻**做锚点，按「包末样本」对齐，得到
     漂移量 `dev = 到达时刻 - 该包末样本的序号/fs`；
  4. 按秒聚合 dev（每秒取中位数）→ 5 秒滚动中位数平滑 → 按序号插值回每个样本；
     实测累积漂移本身很平滑（秒级变化中位数 5 ms），平滑后能把 12 分钟累积
     2.46 s 的漂移压到毫秒级，同时不引入逐点抖动；
  5. 帧间出现 >0.15 s 的跳变视为断点（丢包/重连），单独记进 QC，不再静默压缩。

因此返回的 `t` 是**绝对 epoch 秒**（`t[0]` = 首样本时刻），可直接与心电、视频、
实验分段的时间比。需要相对时间用 `t - qc.t0`。

可选参数 t0/t1（'YYYY-MM-DD HH:MM:SS.fffff' 字符串）用于只读某个时间区间，
数据分析页选段时用得上；不传就是整库。
"""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------- 额定采样率
# 由设备协议决定：手环 250 sps（25 包/秒 × 10 点），腕部三轴随包 25 Hz，心电 250 Hz。
# 写在本模块内、不 import config —— config 在 import 时就会 makedirs(OUTPUT_DIR)，
# 独立脚本为了拿一个常数去引它会到处建目录。
FS_PPG = 250.0
FS_ACC = 25.0
FS_ECG = 250.0
FS_GSR = 10.0      # 皮肤电导：每次上报 10 个值、每秒一次

# 断点判定：相邻包 dev 跳变超过该值（秒）即认为发生过丢包/重连。
# 取 0.15 s：实测正常秒级变化中位数 5 ms、p99 约 0.08 s，0.15 s 不会误报。
JUMP_THR = 0.15
# 漂移曲线的平滑窗口（秒）
SMOOTH_SEC = 5.0


@dataclass
class TimelineQC:
    """时间轴重建的质量报告。"""

    name: str = ''
    n: int = 0                       # 样本数
    fs: float = 0.0                  # 额定采样率（分析用）
    fs_measured: float = 0.0         # 实测速率 = (n-1)/(首末样本时刻差)，低于额定说明在丢样本
    t0: float = float('nan')         # 首样本绝对时刻（epoch 秒）
    duration_idx: float = 0.0        # 样本跨度 = n / fs
    duration_wall: float = 0.0       # 墙钟跨度 = 末锚点 - 首锚点
    drift_s: float = float('nan')    # 墙钟 - 样本（正=有丢包，负=设备时钟略快）
    drift_ppm: float = float('nan')
    n_anchor: int = 0                # 不同时间戳数（=包数）
    rows_per_anchor: float = 0.0
    sec_total: int = 0               # 按到达时刻切的秒段数
    sec_short: int = 0               # 样本数 < 0.8*fs 的秒（投递中断）
    sec_long: int = 0                # 样本数 > 1.8*fs 的秒（补包/积压）
    sec_min: int = 0
    sec_max: int = 0
    sec_median: float = 0.0
    lost_samples: int = 0            # 断点缺口折合的样本数
    gap_seconds: float = 0.0         # 所有断点累计缺口的时长（秒）
    breaks: List[Tuple[int, float, float, float]] = field(default_factory=list)
    max_break_s: float = 0.0
    resid_median_ms: float = float('nan')   # 轴在包锚点处与到达时刻的偏差（中位/95 分位）
    resid_p95_ms: float = float('nan')
    clamped_s: float = 0.0           # 为保持单调而被压掉的累计时长
    status: str = 'ok'               # ok / uniform / single_anchor / empty / unsmoothed

    def summary(self, indent: str = '  ') -> str:
        """一行到两行的人类可读摘要，供命令行打印。"""
        if self.status == 'empty':
            return '{}[时间轴] {} 无数据（fs 按额定 {:.0f} Hz）'.format(
                indent, self.name or '表', self.fs)
        head = ('{}[时间轴] {} n={} fs={:.3f}(实测{:.1f}) 样本跨度={:.1f}s 墙钟={:.1f}s '
                '漂移={:+.3f}s({:+.0f}ppm)').format(
            indent, self.name or '表', self.n, self.fs, self.fs_measured,
            self.duration_idx, self.duration_wall, self.drift_s, self.drift_ppm)
        tail = ('{}锚点={} ({:.1f}行/个)  秒段={} (偏短{} 偏长{})  '
                '断点={}处(最大{:.2f}s, 累计缺口{:.1f}s≈{}样本)').format(
            indent, self.n_anchor, self.rows_per_anchor, self.sec_total,
            self.sec_short, self.sec_long, len(self.breaks),
            self.max_break_s, self.gap_seconds, self.lost_samples)
        if self.status == 'single_anchor':
            tail += '  [只有一个时间戳，退回均匀轴]'
        elif self.status == 'uniform':
            tail += '  [无有效时间戳，退回均匀轴]'
        elif self.status == 'unsmoothed':
            tail += '  [秒段太少，未做漂移平滑]'
        if self.clamped_s > 0.5:
            tail += '  [单调钳制 {:.2f}s]'.format(self.clamped_s)
        return head + '\n' + tail


# ---------------------------------------------------------------- SQL 辅助
def _where(t0, t1):
    """拼时间区间条件。库里 time 是固定格式的字符串，可直接按字典序比较。"""
    if t0 and t1:
        return ' WHERE time >= ? AND time <= ?', (t0, t1)
    if t0:
        return ' WHERE time >= ?', (t0,)
    if t1:
        return ' WHERE time <= ?', (t1,)
    return '', ()


def _open_ro(db_path):
    return sqlite3.connect(f"file:{db_path.replace(chr(92), '/')}?mode=ro", uri=True)


def _fetch_rows(con, table, t0=None, t1=None):
    cond, args = _where(t0, t1)
    try:
        return con.execute(
            'SELECT time, value FROM "{}"{} ORDER BY rowid'.format(table, cond),
            args).fetchall()
    except sqlite3.OperationalError:      # 表不存在
        return []


def _to_epoch(text: str) -> float:
    """库里的时间字符串 → epoch 秒（本地时区，与写入端 now_str() 一致）。"""
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def _median_filter(x: np.ndarray, win: int) -> np.ndarray:
    """一维滚动中位数（边缘按端点延拓），不依赖 scipy。"""
    if win < 3 or len(x) < 3:
        return x
    win = min(int(win) | 1, len(x) | 1)    # 奇数，且不超过序列长度
    if win < 3:
        return x
    half = win // 2
    pad = np.pad(x, (half, half), mode='edge')
    view = np.lib.stride_tricks.sliding_window_view(pad, win)
    return np.median(view, axis=1)


def timeline_from_rows(rows: Sequence, fs: float, name: str = '',
                       jump_thr: float = JUMP_THR,
                       smooth_sec: float = SMOOTH_SEC) -> Tuple[np.ndarray, TimelineQC]:
    """由 (time, value) 行重建**绝对 epoch** 时间轴，返回 (t, qc)。

    `rows` 只要求按写入顺序（rowid）排好、每项第 0 列是时间字符串；value 不参与计算。
    独立脚本（分段特征.py / 实验分段.py 等）可直接把 `_series()` 的结果丢进来。
    """
    fs = float(fs)
    if fs <= 0:
        raise ValueError('fs 必须为正数')
    n = len(rows)
    qc = TimelineQC(name=name, n=n, fs=fs)

    if n == 0:
        qc.status = 'empty'
        return np.empty(0, dtype=np.float64), qc

    # ---- 1. 单遍扫描：只在时间戳变化处解析（每个包一次，不是每行一次）----
    anchor_t: List[float] = []
    anchor_n: List[int] = []
    prev = None
    count = 0
    for item in rows:
        ts = item[0]
        if ts != prev:
            if prev is not None:
                anchor_n.append(count)
            anchor_t.append(_to_epoch(ts))
            prev = ts
            count = 0
        count += 1
    anchor_n.append(count)

    a_t = np.asarray(anchor_t, dtype=np.float64)
    a_n = np.asarray(anchor_n, dtype=np.int64)
    idx_end = np.cumsum(a_n) - 1                      # 每个包末样本的序号
    qc.n_anchor = len(a_t)
    qc.rows_per_anchor = n / float(len(a_t))
    qc.t0 = float(a_t[0] - (a_n[0] - 1) / fs)
    qc.duration_idx = n / fs
    qc.duration_wall = float(a_t[-1] - a_t[0])
    qc.drift_s = qc.duration_wall - qc.duration_idx
    qc.drift_ppm = (qc.drift_s / qc.duration_idx * 1e6
                    if qc.duration_idx > 0 else float('nan'))

    if qc.n_anchor < 2:
        # 只有一个时间戳：无从谈漂移，退回均匀轴（绝不返回空时间轴）
        qc.status = 'single_anchor'
        qc.fs_measured = fs
        qc.drift_s = float('nan')
        qc.drift_ppm = float('nan')
        t = qc.t0 + np.arange(n, dtype=np.float64) / fs
        return t, qc

    # ---- 2. 包级漂移 dev = 到达时刻 − 包末样本序号/fs ----
    # 用包末对齐：到达时刻对应包内最后一个采样点，这样 dev 不含量化台阶
    # （若用包首对齐，心电 80 点/包会产生 0.32 s 的台阶，直接淹没断点阈值）
    dev = a_t - idx_end / fs
    packet_dur = float(np.median(a_n)) / fs        # 一个包覆盖的时长（秒）

    # ---- 3. 每秒聚合（中位数），得到漂移观测序列 ----
    # 桶内取中位数而不是首行：包级到达抖动实测 p5~p95 差 4 倍，
    # 单包做锚点会把 ±50%~100% 的速度抖动带进来。
    sec_key = np.floor(a_t).astype(np.int64)
    _uniq_sec, _start = np.unique(sec_key, return_index=True)
    _edges = np.append(_start, len(sec_key))
    x = np.array([np.median(idx_end[_edges[b]:_edges[b + 1]])
                  for b in range(len(_uniq_sec))], dtype=np.float64)
    y = np.array([np.median(dev[_edges[b]:_edges[b + 1]])
                  for b in range(len(_uniq_sec))], dtype=np.float64)

    # ---- 4. 断点检测（在平滑之前，避免把台阶抹成斜坡）----
    # 阈值下限同时受「包长」约束：心电一个包就是 0.32 s，比这更小的跳变
    # 分辨不出来，否则每秒都能报一个假断点（实测会报 221 处）。
    d_y = np.diff(y)
    noise = float(np.median(np.abs(d_y))) if len(d_y) else 0.0
    thr = max(float(jump_thr), 2.0 * packet_dur, 3.0 * noise)
    cut = np.flatnonzero(np.abs(d_y) > thr) + 1
    bounds = np.concatenate(([0], cut, [len(y)])).astype(int)

    for k in cut:                                  # k 是秒桶序号
        jump = float(y[k] - y[k - 1])
        i0 = int(x[k])
        qc.breaks.append((i0, float(y[k - 1] - qc.t0),
                          float(y[k] - qc.t0), jump))
        qc.gap_seconds += max(0.0, abs(jump) - packet_dur)
    qc.max_break_s = max((abs(b[3]) for b in qc.breaks), default=0.0)

    # ---- 5. 段内滚动中位数平滑（段与段之间不跨断点）----
    y_s = y.copy()
    win = max(3, int(round(smooth_sec)))
    smoothed = False
    for r in range(len(bounds) - 1):
        lo, hi = int(bounds[r]), int(bounds[r + 1])
        if hi - lo >= 5:
            y_s[lo:hi] = _median_filter(y[lo:hi], win)
            smoothed = True
    if not smoothed:
        qc.status = 'unsmoothed'

    # ---- 6. 插值到每个样本 + 单调化 ----
    # 按段分别插值：断点所在的那一个样本处直接跳变（那一段本来就没有数据，
    # 不能摊到前后一秒的样本上，否则断连 35 分钟会被抹成"某 250 个样本变慢"）。
    idx = np.arange(n, dtype=np.float64)
    dev_s = np.empty(n, dtype=np.float64)
    cursor = 0
    last = len(bounds) - 2
    for r in range(len(bounds) - 1):
        lo, hi = int(bounds[r]), int(bounds[r + 1])
        end = n if r == last else min(int(x[hi - 1]) + 1, n)
        if end <= cursor:
            continue
        grid = np.arange(cursor, end, dtype=np.float64)
        dev_s[grid.astype(np.int64)] = np.interp(grid, x[lo:hi], y_s[lo:hi])
        cursor = end
    if cursor < n:                       # 理论上不会发生，兜底
        dev_s[cursor:] = y_s[-1]

    t = idx / fs + dev_s

    raw = t
    t = np.maximum.accumulate(t)
    qc.clamped_s = float(np.max(raw - t)) if n else 0.0
    if n > 1 and np.any(np.diff(t) <= 0):
        # 极端情况下（时钟回拨）补一个 1 ns 的斜坡，保证严格递增
        t = t + idx * 1e-9
    if n > 1 and t[-1] > t[0]:
        qc.fs_measured = float((n - 1) / (t[-1] - t[0]))

    # ---- 7. 实测速率：取最长的一段连续数据算，跨断点会把缺口算进跨度 ----
    best_span, best_fs = -1.0, 0.0
    for r in range(len(bounds) - 1):
        lo, hi = int(bounds[r]), int(bounds[r + 1]) - 1
        dx = float(x[hi] - x[lo])
        if dx <= 0:
            continue
        dy = float(y[hi] - y[lo])
        if dx > best_span and dx / fs + dy > 0:
            best_span = dx
            best_fs = dx / (dx / fs + dy)
    if best_fs > 0:
        qc.fs_measured = float(best_fs)

    # ---- 8. 每秒样本数校验（按到达时刻分桶，与时间轴构造无关）----
    uniq_sec, inv = np.unique(sec_key, return_inverse=True)
    per_sec = np.zeros(len(uniq_sec), dtype=np.int64)
    np.add.at(per_sec, inv, a_n)
    qc.sec_total = int(len(per_sec))
    qc.sec_short = int(np.sum(per_sec < 0.8 * fs))
    qc.sec_long = int(np.sum(per_sec > 1.8 * fs))
    qc.sec_min = int(per_sec.min()) if len(per_sec) else 0
    qc.sec_max = int(per_sec.max()) if len(per_sec) else 0
    qc.sec_median = float(np.median(per_sec)) if len(per_sec) else 0.0
    qc.lost_samples = int(round(qc.gap_seconds * fs))

    # ---- 9. 锚点残差：重建轴在每个包末样本处与「包到达时刻」的偏差 ----
    # 这就是平滑之后的漂移曲线跟设备实际到达时刻差多少（断点附近 2 秒不计，
    # 那里本来就该跳变）。包级到达抖动本身就有几十毫秒，这个数不会比它更小。
    res = (t[idx_end] - a_t) * 1000.0
    if qc.breaks:
        near = np.zeros(len(res), dtype=bool)
        for _i0, _b, _a, _j in qc.breaks:
            near |= np.abs(idx_end - _i0) < fs * 2
        res = res[~near]
    if len(res):
        qc.resid_median_ms = float(np.median(np.abs(res)))
        qc.resid_p95_ms = float(np.percentile(np.abs(res), 95))

    return t, qc


# ---------------------------------------------------------------- 各表加载
def timeline_from_db(db_path, table, fs, t0=None, t1=None):
    """只读 time 列重建时间轴，不解 JSON、不读 value（校对/排查用，快得多）。

    返回 (t, qc)，可用于快速核对任意表（含 gsr / 温度这类没有专用 loader 的表）。
    """
    con = _open_ro(db_path)
    try:
        cond, args = _where(t0, t1)
        rows = con.execute(
            'SELECT time FROM "{}"{} ORDER BY rowid'.format(table, cond),
            args).fetchall()
    finally:
        con.close()
    return timeline_from_rows(rows, fs, table)


def load_ppg(db_path, t0=None, t1=None, with_qc=False):
    """返回 (channels: dict[PPG1..4]->ndarray, fs, t[, qc])。t 为绝对 epoch 秒。"""
    con = _open_ro(db_path)
    try:
        rows = _fetch_rows(con, "bracelet_ppg_data", t0, t1)
    finally:
        con.close()

    n = len(rows)
    ch = {f"PPG{i}": np.empty(n, dtype=np.float64) for i in range(1, 5)}
    for idx, (_ts, v) in enumerate(rows):
        d = json.loads(v)
        for k in ch:
            ch[k][idx] = d.get(k, np.nan)

    t, qc = timeline_from_rows(rows, FS_PPG, name="bracelet_ppg_data")
    if with_qc:
        return ch, qc.fs, t, qc
    return ch, qc.fs, t


def load_accel(db_path, t0=None, t1=None, with_qc=False):
    """返回 (ax, ay, az, fs, t[, qc])。t 为绝对 epoch 秒。"""
    con = _open_ro(db_path)
    try:
        rows = _fetch_rows(con, "bracelet_accel_data", t0, t1)
    finally:
        con.close()

    n = len(rows)
    ax = np.empty(n); ay = np.empty(n); az = np.empty(n)
    for idx, (_ts, v) in enumerate(rows):
        d = json.loads(v)
        ax[idx], ay[idx], az[idx] = d["x"], d["y"], d["z"]

    t, qc = timeline_from_rows(rows, FS_ACC, name="bracelet_accel_data")
    if with_qc:
        return ax, ay, az, qc.fs, t, qc
    return ax, ay, az, qc.fs, t


def load_ecg(db_path, t0=None, t1=None, with_qc=False):
    """返回 (ecg, fs, t[, qc])。t 为绝对 epoch 秒。"""
    con = _open_ro(db_path)
    try:
        rows = _fetch_rows(con, "ecg_data", t0, t1)
    finally:
        con.close()

    ecg = np.array([float(v) for (_ts, v) in rows], dtype=np.float64)
    t, qc = timeline_from_rows(rows, FS_ECG, name="ecg_data")
    if with_qc:
        return ecg, qc.fs, t, qc
    return ecg, qc.fs, t
