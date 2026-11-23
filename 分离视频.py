"""把会话库拆成「信号库」和「视频源」两半。

问题
    一场会话的 .db 动辄 3~9 GB，其中 96% 是摄像头 JPEG。只做心电/皮电分析
    也要把整个库打开，翻页、查询、拷贝都很别扭。

做法（不破坏原库）
    在 信号库\\ 下生成一个同名小库：所有时序信号 + 摄像头帧索引（不含 JPEG）。
    视频本体继续留在原库里，信号库里记下它的路径，需要某一帧时按索引回取。

    信号库\\<组别>_<会话>.db
        ecg_data / bracelet_* / temperature_data / triaxial_data  —— 原样复制
        camera_index(time, camera, frame_index, width, height)    —— 只有索引
        camera_sessions                                           —— 元数据（帧率、分辨率）
        meta(source_db, camera_db, split_at, rows_*)              —— 指向视频源

用法
    python 分离视频.py                 # 全部会话
    python 分离视频.py --only 高晨瑞    # 只处理某位被试
    python 分离视频.py --force         # 覆盖已存在的信号库
"""

import argparse
import csv
import glob
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "AAAwindows_code", "data")
OUT_DIR = os.path.join(ROOT, "信号库")

SIGNAL_TABLES = ["ecg_data", "bracelet_ppg_data", "bracelet_accel_data",
                 "bracelet_gsr_data", "bracelet_skin_temp", "bracelet_battery",
                 "temperature_data", "triaxial_data"]


def sessions():
    out = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "**", "*.db"), recursive=True)):
        size = os.path.getsize(path)
        if size < 10 * 1024 * 1024:            # 心算这类小库不用拆
            continue
        out.append((path, size))
    return out


def table_rows(con, schema, table):
    try:
        return con.execute('select count(*) from "%s"."%s"' % (schema, table)).fetchone()[0]
    except sqlite3.Error:
        return None


def split_one(src, dst):
    """用 ATTACH + CREATE TABLE AS SELECT 复制，避免把数据搬到 Python 里。"""
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(src)
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("ATTACH DATABASE ? AS sig", (dst,))

    have = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'")}
    copied = {}
    for t in SIGNAL_TABLES:
        if t not in have:
            continue
        con.execute('CREATE TABLE sig."%s" AS SELECT * FROM main."%s"' % (t, t))
        # 时间索引：按时间段切片是最高频的操作，有索引后不必整表扫描
        con.execute('CREATE INDEX sig.idx_%s_time ON "%s" (time)' % (t, t))
        copied[t] = table_rows(con, "sig", t)

    if "camera_frames" in have:
        con.execute("CREATE TABLE sig.camera_index AS "
                    "SELECT time, camera, frame_index, width, height FROM main.camera_frames")
        con.execute("CREATE INDEX sig.idx_camera_index_time ON camera_index (time)")
        copied["camera_index"] = table_rows(con, "sig", "camera_index")
    if "camera_sessions" in have:
        con.execute("CREATE TABLE sig.camera_sessions AS SELECT * FROM main.camera_sessions")

    con.execute("CREATE TABLE sig.meta (key TEXT, value TEXT)")
    meta = [("source_db", os.path.abspath(src)),
            ("camera_db", os.path.abspath(src)),
            ("split_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("note", "视频帧仍在 camera_db 的 camera_frames 表；"
                     "按 camera_index.frame_index 回取")]
    for k, v in copied.items():
        meta.append(("rows_" + k, str(v)))
    con.executemany("INSERT INTO sig.meta VALUES (?,?)", meta)

    con.commit()
    con.execute("DETACH DATABASE sig")
    con.close()
    return copied


def verify(src, dst, copied):
    """逐表核对行数。"""
    a = sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/"), uri=True)
    b = sqlite3.connect("file:%s?mode=ro" % dst.replace("\\", "/"), uri=True)
    bad = []
    for t, n in copied.items():
        real = "camera_frames" if t == "camera_index" else t
        try:
            m = a.execute('select count(*) from "%s"' % real).fetchone()[0]
        except sqlite3.Error:
            m = None
        got = b.execute('select count(*) from "%s"' % t).fetchone()[0]
        if m != got:
            bad.append((t, m, got))
    # 抽查一帧的索引是否对得上
    try:
        r1 = a.execute("select time, frame_index from camera_frames order by rowid desc limit 1").fetchone()
        r2 = b.execute("select time, frame_index from camera_index order by rowid desc limit 1").fetchone()
        if r1 != r2:
            bad.append(("camera_index 末行", r1, r2))
    except sqlite3.Error:
        pass
    a.close()
    b.close()
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只处理姓名/目录名包含该字符串的会话")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的信号库")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    items = sessions()
    if args.only:
        items = [(p, s) for p, s in items if args.only in p]
    print("待处理 %d 个会话" % len(items))

    rows = []
    for src, size in items:
        label = os.path.basename(os.path.dirname(os.path.dirname(src)))   # 晁超_能见度
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(OUT_DIR, "%s_%s.db" % (label, stem))
        if os.path.exists(dst) and not args.force:
            print("  跳过（已存在）：%s" % os.path.basename(dst))
            rows.append(dict(信号库=os.path.basename(dst), 原始GB=round(size / 1073741824, 2),
                             信号MB=round(os.path.getsize(dst) / 1048576, 1),
                             压缩比="%.0f%%" % (100.0 * os.path.getsize(dst) / size),
                             心电行="", 视频帧="", 状态="已存在"))
            continue

        t0 = time.time()
        print("  拆分 %s（%.2f GB）…" % (label, size / 1073741824), end=" ", flush=True)
        copied = split_one(src, dst)
        bad = verify(src, dst, copied)
        dur = time.time() - t0
        newsize = os.path.getsize(dst)
        status = "OK" if not bad else "行数不一致:" + str(bad)
        print("%.0f MB（%.0f%%）%s  %.1fs" % (newsize / 1048576, 100.0 * newsize / size,
                                              "" if not bad else "  ✗" + str(bad), dur))
        rows.append(dict(信号库=os.path.basename(dst), 原始GB=round(size / 1073741824, 2),
                         信号MB=round(newsize / 1048576, 1),
                         压缩比="%.0f%%" % (100.0 * newsize / size),
                         心电行=copied.get("ecg_data", ""),
                         视频帧=copied.get("camera_index", ""),
                         状态=status))

    csv_path = os.path.join(OUT_DIR, "分离索引.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total_src = sum(r["原始GB"] for r in rows if r["原始GB"] < 100)
    total_new = sum(r["信号MB"] for r in rows) / 1024.0
    print("\n信号库合计 %.2f GB（原库合计 %.2f GB）" % (total_new, total_src))
    print("索引：%s" % csv_path)


if __name__ == "__main__":
    main()
