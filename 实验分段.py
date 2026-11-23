"""按《实验设置》表把连续采集的会话库切成一个个飞行任务段。

数据来源
    C:\\Users\\lenovo\\Desktop\\实验设置\\<姓名>\\<第N组>实验设置.xlsx
    每个表：一次五边飞行（起落航线）一行，含起止时刻、能见度/侧风/单发失效、结果备注。

输出
    实验分段\\
      ├─ 分段索引.csv        一次任务一行（宽表，可直接进 SPSS / Excel）
      ├─ 实验分段.db         trials（任务清单）+ trial_features（长表指标）+ trial_qc
      └─ <姓名>\\<组别>\\T03_1631-1643\\
           ├─ trial.db       该段全部信号（不含视频本体，只存帧索引）
           └─ trial.json     元数据：来源库、时段、设置、备注、通道行数

用法
    python 实验分段.py                 # 全部切分 + 出指标
    python 实验分段.py --no-export     # 只出索引和指标，不导每段的小库
    python 实验分段.py --with-camera   # 连视频帧一起导（体积大，默认不导）
"""

import argparse
import csv
import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

import numpy as np
from scipy import signal as sg

from ppg_pipeline.data_loader import timeline_from_rows, FS_ECG, FS_GSR

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "AAAwindows_code", "data")
SETUP_DIR = r"C:\Users\lenovo\Desktop\实验设置"
OUT_DIR = os.path.join(ROOT, "实验分段")

SIGNAL_TABLES = ["ecg_data", "bracelet_ppg_data", "bracelet_accel_data",
                 "bracelet_gsr_data", "bracelet_skin_temp", "bracelet_battery",
                 "temperature_data", "triaxial_data"]


# ---------------------------------------------------------------- 读设置表
def _parse_times(text):
    """从 '15:13——15:19' 这类单元格里取出起止时刻。"""
    if not text:
        return None
    hits = re.findall(r"(\d{1,2})\s*[:：]\s*(\d{2})", str(text))
    if len(hits) < 2:
        return None
    (h1, m1), (h2, m2) = hits[0], hits[1]
    return int(h1), int(m1), int(h2), int(m2)


def read_setup_files():
    """返回任务清单：[{subject, group, task, condition, start, end, setting, note, ...}]"""
    import openpyxl

    items = []
    for path in sorted(glob.glob(os.path.join(SETUP_DIR, "**", "*.xlsx"), recursive=True)):
        folder = os.path.basename(os.path.dirname(path))        # 飞行员晁超 / 飞行学员高晨瑞
        subject = re.sub(r"^(飞行员|飞行学员)", "", folder).strip()
        group = os.path.splitext(os.path.basename(path))[0].replace("实验设置", "")

        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        title = next((str(c) for c in rows[0] if c), "").strip() if rows else ""
        condition = ""
        m = re.search(r"[（(](.+?)[)）]", title)
        if m:
            condition = m.group(1).strip()

        date = None
        for row in rows[:3]:
            for cell in row:
                mm = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", str(cell or ""))
                if mm:
                    date = datetime(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
                    break
            if date:
                break
        if date is None:
            continue

        for row in rows[3:]:
            if not row or row[0] is None:
                continue
            try:
                task = int(str(row[0]).strip())
            except ValueError:
                continue
            parsed = _parse_times(row[1])
            if parsed is None:
                continue
            h1, m1, h2, m2 = parsed
            start = date.replace(hour=h1, minute=m1)
            end = date.replace(hour=h2, minute=m2)
            fixed = ""
            if end < start:
                end += timedelta(days=1)
            if (end - start) > timedelta(minutes=90) and h2 != h1:
                # 明显的录入笔误（如 10:20——18:26），按同一小时修正
                end = start.replace(minute=m2) if m2 >= m1 else start + timedelta(minutes=m2 - m1)
                fixed = "结束时刻按录入笔误修正"

            setting = str(row[2]).strip() if len(row) > 2 and row[2] else ""
            note = str(row[3]).strip() if len(row) > 3 and row[3] else ""
            items.append(dict(subject=subject, group=group, task=task,
                              condition=condition, start=start, end=end,
                              setting=setting, note=note, fixed=fixed,
                              source=os.path.relpath(path, SETUP_DIR)))
    items.sort(key=lambda x: (x["subject"], x["start"]))
    return items


# ---------------------------------------------------------------- 设计变量解析
def parse_design(setting):
    """把 '第二边飞行中左发失效+东风10m/s+能见度5000' 拆成结构化因子。"""
    s = (setting or "").replace(" ", "")
    vis = re.search(r"能见度[：:]?\s*(\d+)", s)
    wind = re.search(r"([东南西北])风\s*(-?\d+)\s*m/s", s)
    fail = "左发失效" in s or "右发失效" in s
    phase = ""
    if fail:
        m = re.search(r"([一二三四五]边(?:转[一二三四五]边)?|一边转二边)", s)
        phase = m.group(1) if m else ""

    vis_m = int(vis.group(1)) if vis else None
    wind_dir = wind.group(1) + "风" if wind else ""
    wind_speed = int(wind.group(2)) if wind else None

    cues = []
    if vis_m is not None and vis_m < 1000000:
        cues.append("低能见度")
    if wind_speed:
        cues.append("侧风")
    if fail:
        cues.append("单发失效")

    # 条件分类：单发失效一律归到「复合」。
    # 单发失效本身就不是"加一个环境变量"那么简单——它同时改变了操纵与处置流程，
    # 而且样本只有晁超一组 3 段，单独成类既没统计意义、又会和"复合"重复计数。
    if fail:
        kind = "复合"
    elif not cues:
        kind = "基线"
    elif len(cues) == 1:
        kind = cues[0]
    else:
        kind = "复合"
    return dict(vis_m=vis_m, wind_dir=wind_dir, wind_speed=wind_speed,
                failure=int(fail), failure_phase=phase, kind=kind)


# ---------------------------------------------------------------- 会话库匹配
def list_sessions():
    out = []
    for path in glob.glob(os.path.join(DATA_DIR, "**", "*.db"), recursive=True):
        if os.path.getsize(path) < 1048576:      # 跳过心算这类小库
            continue
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        try:
            lo, hi = con.execute("select min(time), max(time) from ecg_data").fetchone()
        except sqlite3.Error:
            con.close()
            continue
        con.close()
        if not lo:
            continue
        out.append(dict(path=path, subject=os.path.basename(os.path.dirname(os.path.dirname(path))).split("_")[0],
                        label=os.path.basename(os.path.dirname(os.path.dirname(path))),
                        t0=datetime.fromisoformat(lo), t1=datetime.fromisoformat(hi)))
    return out


def match_session(item, sessions):
    """按时间和姓名双重匹配：同一被试、重叠最多者。"""
    best, best_ov = None, 0.0
    for s in sessions:
        ov = (min(item["end"], s["t1"]) - max(item["start"], s["t0"])).total_seconds()
        if ov <= 0:
            continue
        if item["subject"] not in s["label"] and item["subject"] not in s["subject"]:
            continue
        if ov > best_ov:
            best, best_ov = s, ov
    return best, best_ov


# ---------------------------------------------------------------- 指标
def _get(con, table, t0, t1, cols="time, value"):
    try:
        return con.execute(
            'select %s from "%s" where time >= ? and time < ? order by rowid'
            % (cols, table), (t0, t1)).fetchall()
    except sqlite3.Error:
        return []


def ecg_metrics(con, t0, t1):
    """心电指标。返回的 ecg_note 说明这一段为什么可用/不可用。"""
    rows = _get(con, "ecg_data", t0, t1)
    out = dict(ecg_rows=len(rows), ecg_fs=np.nan, hr_mean=np.nan, hr_sd=np.nan,
               hr_min=np.nan, hr_max=np.nan, sdnn=np.nan, rmssd=np.nan,
               pnn50=np.nan, lf_hf=np.nan, r_peaks=0, ecg_valid=0,
               ecg_amp=np.nan, ecg_note="")
    if len(rows) < 500:
        out["ecg_note"] = "无数据（%d 行）" % len(rows)
        return out

    v = np.array([float(r[1]) for r in rows])

    # 采样率按额定值、时间轴按包锚点重建（见 ppg_pipeline/data_loader.py 的说明）。
    # 以前这里按 (行数-1)/墙钟跨度 估，整包共用时间戳或中途断连时会跑偏。
    _t, qc = timeline_from_rows(rows, FS_ECG, "ecg_data")
    span = qc.duration_wall
    fs = qc.fs
    out["ecg_fs"] = fs

    # 幅度门槛：电极脱开时信号只剩 ±1 的噪声，raw std 会掉到 1 以下
    if v.std() < 5.0:
        out["ecg_note"] = "信号幅度过小（std=%.1f），电极未接触" % v.std()
        return out

    hi = min(40.0, fs * 0.45)
    if hi <= 3.5:
        out["ecg_note"] = "采样率异常（%.0f Hz）" % fs
        return out
    y = sg.filtfilt(*sg.butter(2, [3 / (fs / 2), hi / (fs / 2)], btype="band"),
                    sg.detrend(v))
    amp = float(np.percentile(y, 99) - np.percentile(y, 1))
    out["ecg_amp"] = amp
    if amp < 100:
        out["ecg_note"] = "心电幅度过小（带通后 %.0f）" % amp
        return out

    pk, _ = sg.find_peaks(y, prominence=1.0 * y.std(), distance=int(fs * 0.33))
    out["r_peaks"] = len(pk)
    if len(pk) < 10:
        out["ecg_note"] = "只检出 %d 个 R 峰" % len(pk)
        return out

    rr = np.diff(pk) / fs
    for _ in range(4):                         # 剔除漏检/多检造成的离群间期
        med = np.median(rr)
        keep = (rr > 0.55 * med) & (rr < 1.6 * med)
        if keep.all() or keep.sum() < 10:
            break
        rr = rr[keep]
    if len(rr) < 10:
        out["ecg_note"] = "有效间期不足"
        return out

    hr = 60.0 / rr
    cv = rr.std() / rr.mean()
    covered = rr.sum() / max(span, 1e-9)
    if not (45 <= np.median(hr) <= 150):
        out["ecg_note"] = "心率越界（%.0f bpm）" % np.median(hr)
        return out
    if cv > 0.25:
        out["ecg_note"] = "节律不规整（RR CV=%.2f），疑似伪信号" % cv
        return out
    if covered < 0.5:
        out["ecg_note"] = "心搏覆盖不足（%.0f%%）" % (100 * covered)
        return out

    out.update(ecg_valid=1, hr_mean=float(hr.mean()), hr_sd=float(hr.std()),
               hr_min=float(np.percentile(hr, 5)), hr_max=float(np.percentile(hr, 95)))
    out["ecg_note"] = "ok"

    rrs = rr * 1000.0
    out["sdnn"] = float(np.std(rrs, ddof=1))
    out["rmssd"] = float(np.sqrt(np.mean(np.diff(rrs) ** 2)))
    out["pnn50"] = float(100.0 * np.mean(np.abs(np.diff(rrs)) > 50))

    if len(rrs) >= 60:                         # 频域：4 Hz 重采样后 Welch
        t_rr = np.cumsum(rr)
        grid = np.arange(0, t_rr[-1], 0.25)
        if len(grid) > 16:
            interp = np.interp(grid, t_rr[1:], rrs[1:])
            f, p = sg.welch(interp - interp.mean(), fs=4.0,
                            nperseg=min(256, len(interp)))
            lf = p[(f >= 0.04) & (f < 0.15)].sum()
            hf = p[(f >= 0.15) & (f < 0.4)].sum()
            if hf > 0:
                out["lf_hf"] = float(lf / hf)
    return out


def acc_metrics(con, t0, t1):
    out = dict(acc_rows=0, acc_mean=np.nan, acc_sd=np.nan, acc_active=np.nan)
    rows = _get(con, "bracelet_accel_data", t0, t1)
    if len(rows) < 50:
        return out
    # 手环加速度不一定是 JSON，也可能是三轴表
    vals = []
    for _, raw in rows:
        try:
            d = json.loads(raw)
            vals.append((d["x"], d["y"], d["z"]))
        except Exception:
            pass
    if len(vals) < 50:
        tri = _get(con, "triaxial_data", t0, t1)
        for _, raw in tri:
            try:
                d = json.loads(raw)
                vals.append((d["x"], d["y"], d["z"]))
            except Exception:
                pass
    if not vals:
        return out
    arr = np.array(vals, dtype=float)
    mag = np.linalg.norm(arr, axis=1)
    dev = np.abs(mag - np.median(mag))
    out.update(acc_rows=len(arr), acc_mean=float(mag.mean()), acc_sd=float(mag.std()),
               acc_active=float(np.mean(dev > 0.05)))
    return out


def gsr_metrics(con, t0, t1):
    out = dict(gsr_rows=0, scl_mean=np.nan, scl_range=np.nan, scr_count=np.nan, gsr_valid=0)
    rows = _get(con, "bracelet_gsr_data", t0, t1)
    if len(rows) < 50:
        return out
    g = np.array([float(r[1]) for r in rows])
    _t, qc = timeline_from_rows(rows, FS_GSR, "bracelet_gsr_data")
    fs = qc.fs
    out.update(gsr_rows=len(g), scl_mean=float(g.mean()),
               scl_range=float(np.percentile(g, 95) - np.percentile(g, 5)))
    # 全程恒定 / 线性放电（电极浮空）都判无效
    if g.std() < 1e-4:
        return out
    if len(g) > 100:
        k = np.polyfit(np.arange(len(g)) / fs, g, 1)[0]
        resid = g - np.polyval(np.polyfit(np.arange(len(g)) / fs, g, 1), np.arange(len(g)) / fs)
        if abs(k) > 1e-5 and resid.std() < 0.01 * (g.max() - g.min() + 1e-9):
            return out                              # 纯线性漂移，不是皮肤电
    out["gsr_valid"] = 1
    # SCR：0.5 Hz 低通后找峰
    if fs >= 2:
        smooth = sg.filtfilt(*sg.butter(2, min(0.5, fs * 0.4) / (fs / 2)), g - g.mean())
        pk, _ = sg.find_peaks(smooth, prominence=0.01, distance=int(fs * 1.0))
        out["scr_count"] = len(pk)
    return out


def temp_metrics(con, t0, t1):
    rows = _get(con, "temperature_data", t0, t1)
    if len(rows) < 20:
        return dict(temp_rows=0, temp_mean=np.nan, temp_min=np.nan, temp_max=np.nan)
    v = np.array([float(r[1]) for r in rows])
    return dict(temp_rows=len(v), temp_mean=float(v.mean()),
                temp_min=float(v.min()), temp_max=float(v.max()))


def camera_metrics(con, t0, t1):
    try:
        n = con.execute("select count(*) from camera_frames where time >= ? and time < ?",
                        (t0, t1)).fetchone()[0]
    except sqlite3.Error:
        return dict(cam_frames=0, cam_fps=np.nan)
    span = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds()
    return dict(cam_frames=n, cam_fps=(n / span) if span > 0 else np.nan)


# ---------------------------------------------------------------- 导出单个任务段
def export_trial(item, src_path, t0, t1, trial_id, with_camera=False):
    folder = os.path.join(OUT_DIR, item["subject"], item["group"],
                          "T%02d_%s-%s" % (item["task"], t0[11:16].replace(":", ""),
                                           t1[11:16].replace(":", "")))
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, "trial.db")
    if os.path.exists(dst):
        os.remove(dst)

    con = sqlite3.connect("file:%s?mode=ro" % src_path.replace("\\", "/"), uri=True)
    out = sqlite3.connect(dst)
    has_tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    counts = {}
    for table in SIGNAL_TABLES:
        if table not in has_tables:
            continue
        rows = con.execute('select time, value from "%s" where time >= ? and time < ? order by rowid'
                           % table, (t0, t1)).fetchall()
        out.execute('CREATE TABLE "%s" (time TEXT, value TEXT)' % table)
        out.executemany('INSERT INTO "%s" (time, value) VALUES (?,?)' % table, rows)
        counts[table] = len(rows)

    # 视频只存索引（时间 + 帧号），本体留在原库/原文件夹，按需回取
    if "camera_frames" in has_tables:
        rows = con.execute("select time, camera, frame_index, width, height from camera_frames "
                           "where time >= ? and time < ? order by rowid", (t0, t1)).fetchall()
        out.execute("CREATE TABLE camera_index (time TEXT, camera TEXT, frame_index INTEGER, "
                    "width INTEGER, height INTEGER)")
        out.executemany("INSERT INTO camera_index VALUES (?,?,?,?,?)", rows)
        counts["camera_index"] = len(rows)
        if with_camera:
            # 新格式的库里没有 JPEG，只有文件名；这里把图复制进 trial 目录，
            # 让导出的每段小库自包含（拷走 trial 文件夹就能单独看）
            cols = {r[1] for r in con.execute('PRAGMA table_info("camera_frames")')}
            has_file = "file" in cols
            sql = ("select time, frame_index, width, height, jpeg%s from camera_frames "
                   "where time >= ? and time < ? order by rowid"
                   % (", file" if has_file else ""))
            src_dir = os.path.dirname(os.path.abspath(src_path))
            cam_dir = os.path.join(folder, "camera")
            kept = []
            for rec in con.execute(sql, (t0, t1)):
                stamp, idx, width, height, blob = rec[0], rec[1], rec[2], rec[3], rec[4]
                if not blob and has_file and rec[5]:
                    rel = rec[5]
                    path = rel if os.path.isabs(rel) else os.path.join(src_dir, *rel.split("/"))
                    try:
                        with open(path, "rb") as fh:
                            blob = fh.read()
                    except OSError:
                        blob = None
                if not blob:
                    continue
                if not kept:
                    os.makedirs(cam_dir, exist_ok=True)
                name = "%06d.jpg" % int(idx)
                with open(os.path.join(cam_dir, name), "wb") as fh:
                    fh.write(blob)
                kept.append((stamp, int(idx), width, height, b"", "camera/" + name))
            out.execute("CREATE TABLE camera_frames (time TEXT, frame_index INTEGER, "
                        "width INTEGER, height INTEGER, jpeg BLOB, file TEXT)")
            out.executemany("INSERT INTO camera_frames VALUES (?,?,?,?,?,?)", kept)
            counts["camera_frames"] = len(kept)

    out.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    meta = dict(trial_id=str(trial_id), subject=item["subject"], group=item["group"],
                task=str(item["task"]), condition=item["condition"],
                setting=item["setting"], note=item["note"],
                start=t0, end=t1, source_db=src_path, setup_file=item["source"])
    out.executemany("INSERT INTO meta VALUES (?,?)", [(k, v) for k, v in meta.items()])
    out.commit()
    out.close()
    con.close()

    with open(os.path.join(folder, "trial.json"), "w", encoding="utf-8") as f:
        json.dump(dict(meta, tables=counts), f, ensure_ascii=False, indent=2)
    return folder, counts


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-export", action="store_true", help="只出索引和指标，不导每段小库")
    ap.add_argument("--with-camera", action="store_true", help="连视频 JPEG 一起导（很占空间）")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    items = read_setup_files()
    sessions = list_sessions()
    print("设置表任务：%d 条；会话库：%d 个" % (len(items), len(sessions)))

    master = os.path.join(OUT_DIR, "实验分段.db")
    if os.path.exists(master):
        os.remove(master)
    mcon = sqlite3.connect(master)
    mcon.executescript("""
        CREATE TABLE trials (id INTEGER PRIMARY KEY, subject TEXT, grp TEXT, task INTEGER,
            condition TEXT, setting TEXT, note TEXT, start TEXT, end TEXT,
            duration_min REAL, source_db TEXT, overlap_ratio REAL, flag TEXT,
            kind TEXT, vis_m INTEGER, wind_dir TEXT, wind_speed INTEGER,
            failure INTEGER, failure_phase TEXT);
        CREATE TABLE trial_features (trial_id INTEGER, channel TEXT, metric TEXT, value REAL);
        CREATE TABLE trial_qc (trial_id INTEGER, channel TEXT, status TEXT, detail TEXT);
    """)

    rows_csv = []
    tid = 0
    for item in items:
        sess, ov = match_session(item, sessions)
        start = item["start"].strftime("%Y-%m-%d %H:%M:%S")
        end = item["end"].strftime("%Y-%m-%d %H:%M:%S")
        flag = item["fixed"]
        if sess is None:
            print("  ✗ 未匹配到会话库：%s %s 第%d组 %s" % (item["subject"], item["group"], item["task"], start))
            continue

        span = (item["end"] - item["start"]).total_seconds()
        ratio = ov / span if span else 0
        t0, t1 = start, end
        if item["start"] < sess["t0"]:
            t0 = sess["t0"].strftime("%Y-%m-%d %H:%M:%S")
            flag = (flag + "；" if flag else "") + "起点早于采集，已截断"
        if item["end"] > sess["t1"]:
            t1 = sess["t1"].strftime("%Y-%m-%d %H:%M:%S")
            flag = (flag + "；" if flag else "") + "终点晚于采集，已截断"

        con = sqlite3.connect("file:%s?mode=ro" % sess["path"].replace("\\", "/"), uri=True)
        feats = {}
        feats.update(ecg_metrics(con, t0, t1))
        feats.update(acc_metrics(con, t0, t1))
        feats.update(gsr_metrics(con, t0, t1))
        feats.update(temp_metrics(con, t0, t1))
        feats.update(camera_metrics(con, t0, t1))
        con.close()

        tid += 1
        dur = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 60.0
        d = parse_design(item["setting"])
        mcon.execute("INSERT INTO trials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (tid, item["subject"], item["group"], item["task"], item["condition"],
                      item["setting"], item["note"], t0, t1, round(dur, 2),
                      os.path.relpath(sess["path"], ROOT), round(ratio, 3), flag,
                      d["kind"], d["vis_m"], d["wind_dir"], d["wind_speed"],
                      d["failure"], d["failure_phase"]))
        for k, v in feats.items():
            if not isinstance(v, (int, float, np.integer, np.floating)):
                continue                     # ecg_note 这类文字说明不写进数值表
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            ch = ("ECG" if k.startswith(("ecg", "hr_", "sdnn", "rmssd", "pnn", "lf_", "r_"))
                  else "ACC" if k.startswith("acc") else "GSR" if k.startswith(("gsr", "scl", "scr"))
                  else "TEMP" if k.startswith("temp") else "CAM" if k.startswith("cam") else "OTHER")
            mcon.execute("INSERT INTO trial_features VALUES (?,?,?,?)", (tid, ch, k, float(v)))

        for ch, ok, detail in [
                ("ECG", feats["ecg_valid"], "%s（%d 个 R 峰，%.0f Hz）"
                 % (feats["ecg_note"], feats["r_peaks"], feats["ecg_fs"])
                 if not np.isnan(feats["ecg_fs"]) else feats["ecg_note"]),
                ("GSR", feats["gsr_valid"], "%d 点" % feats["gsr_rows"]),
                ("ACC", int(feats["acc_rows"] > 50), "%d 点" % feats["acc_rows"]),
                ("CAM", int(feats["cam_frames"] > 100), "%d 帧" % feats["cam_frames"])]:
            mcon.execute("INSERT INTO trial_qc VALUES (?,?,?,?)",
                         (tid, ch, "ok" if ok else "无效", detail))

        folder = os.path.join(OUT_DIR, item["subject"], item["group"],
                              "T%02d_%s-%s" % (item["task"], t0[11:16].replace(":", ""),
                                               t1[11:16].replace(":", "")))
        if not args.no_export:
            export_trial(item, sess["path"], t0, t1, tid, args.with_camera)

        rows_csv.append(dict(
            任务编号=tid, 姓名=item["subject"], 组别=item["group"], 任务序号=item["task"],
            实验条件=item["condition"], 条件类型=d["kind"],
            能见度m=d["vis_m"] if d["vis_m"] is not None else "",
            风向=d["wind_dir"], 风速=d["wind_speed"] if d["wind_speed"] is not None else "",
            单发失效="是" if d["failure"] else "", 失效阶段=d["failure_phase"],
            设置=item["setting"], 结果备注=item["note"],
            开始=t0, 结束=t1, 时长min=round(dur, 1),
            心电可用="是" if feats["ecg_valid"] else "否",
            平均心率=round(feats["hr_mean"], 1) if feats["ecg_valid"] else "",
            心率SD=round(feats["hr_sd"], 1) if feats["ecg_valid"] else "",
            心率最低=round(feats["hr_min"], 1) if feats["ecg_valid"] else "",
            心率最高=round(feats["hr_max"], 1) if feats["ecg_valid"] else "",
            SDNN=round(feats["sdnn"], 1) if feats["ecg_valid"] else "",
            RMSSD=round(feats["rmssd"], 1) if feats["ecg_valid"] else "",
            pNN50=round(feats["pnn50"], 1) if feats["ecg_valid"] else "",
            LFHF=round(feats["lf_hf"], 2) if not np.isnan(feats["lf_hf"]) else "",
            活动占比=round(feats["acc_active"], 3) if not np.isnan(feats["acc_active"]) else "",
            SCL均值=round(feats["scl_mean"], 3) if feats["gsr_valid"] else "",
            SCR次数=feats["scr_count"] if feats["gsr_valid"] else "",
            体温均值=round(feats["temp_mean"], 2) if not np.isnan(feats["temp_mean"]) else "",
            视频帧数=feats["cam_frames"], 视频fps=round(feats["cam_fps"], 1) if not np.isnan(feats["cam_fps"]) else "",
            来源库=os.path.relpath(sess["path"], ROOT), 重合度=round(ratio, 2),
            标记=flag, 分段目录=os.path.relpath(folder, ROOT) if folder else ""))

        print("  ✓ T%02d %-4s %-4s %s–%s  心电%s  %s" %
              (item["task"], item["subject"][:4], item["group"],
               t0[11:16], t1[11:16], "OK " if feats["ecg_valid"] else "—  ",
               ("%.0f bpm" % feats["hr_mean"]) if feats["ecg_valid"] else ""))

    mcon.commit()
    mcon.close()

    csv_path = os.path.join(OUT_DIR, "分段索引.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_csv[0].keys()))
        w.writeheader()
        w.writerows(rows_csv)

    ok = sum(1 for r in rows_csv if r["心电可用"] == "是")
    print("\n完成：%d 个任务段，心电可用 %d 段" % (len(rows_csv), ok))
    print("索引：%s" % csv_path)
    print("库　：%s" % master)


if __name__ == "__main__":
    main()
