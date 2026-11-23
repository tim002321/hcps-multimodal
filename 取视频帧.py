"""按时间段从视频库里取帧，存成 JPEG 文件。

信号库只存了帧索引（时间 + 帧号），视频本体还在原会话库里。要拿某几帧时用这个脚本，
不必把整库 8 GB 读进内存。

用法
    # 取 19:05:00–19:05:05 的帧，每秒 5 帧
    python 取视频帧.py 信号库\\高晨瑞_复合_20261106_185039.db --from 19:05:00 --to 19:05:05 --fps 5

    # 只取单帧（按帧号）
    python 取视频帧.py 信号库\\高晨瑞_复合_20261106_185039.db --index 12345

    # 指定会话库（不经过信号库也行）
    python 取视频帧.py AAAwindows_code\\data\\...\\20261106_185039.db --from 18:56:00 --to 18:56:03

输出
    默认写到 取帧\\<会话名>\\<时间>_<帧号>.jpg，同时生成 frames.csv（时间/帧号/文件/字节）
"""

import argparse
import csv
import os
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(ROOT, "取帧")


def _meta(db):
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    info = {}
    if "meta" in tabs:
        info = dict(con.execute("select key, value from meta"))
    has_frames = "camera_frames" in tabs
    has_index = "camera_index" in tabs
    con.close()
    return info, has_frames, has_index


def resolve_paths(db):
    """返回 (帧索引来源, 视频库路径)。信号库会给 camera_db；原库本身就是视频库。"""
    info, has_frames, has_index = _meta(db)
    video_db = info.get("camera_db") or db
    if has_index:
        return db, video_db
    if has_frames:
        return db, db
    raise SystemExit("这个库里没有 camera_index，也没有 camera_frames：%s" % db)


def pick_frames(index_db, t0, t1, fps, limit):
    con = sqlite3.connect("file:%s?mode=ro" % index_db.replace("\\", "/"), uri=True)
    tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    table = "camera_index" if "camera_index" in tabs else "camera_frames"
    if t0 and t1:
        rows = con.execute('select time, frame_index from "%s" where time >= ? and time < ? '
                           'order by frame_index' % table, (t0, t1)).fetchall()
    else:
        rows = con.execute('select time, frame_index from "%s" order by frame_index limit 50'
                           % table).fetchall()
    con.close()
    if not rows:
        return []

    if fps and fps > 0 and len(rows) > 2:
        from datetime import datetime
        step = max(1, int(round(1.0 / fps / ((datetime.fromisoformat(rows[-1][0]) -
                                             datetime.fromisoformat(rows[0][0])).total_seconds()
                                            / max(len(rows) - 1, 1)))))
        rows = rows[::step]
    if limit:
        rows = rows[:limit]
    return rows


def fetch(db, frame_indices):
    """按 frame_index 批量取 JPEG（该列有索引，不会全表扫）。

    新格式库里 jpeg 是空的、图片在 file 指向的文件夹里（相对库所在目录），
    这里统一读成字节返回；老格式直接取 BLOB。
    """
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    cols = {r[1] for r in con.execute('PRAGMA table_info("camera_frames")')}
    has_file = "file" in cols
    base = os.path.dirname(os.path.abspath(db))
    out = {}
    CH = 200
    for i in range(0, len(frame_indices), CH):
        chunk = frame_indices[i:i + CH]
        q = ('select frame_index, jpeg%s from camera_frames where frame_index in (%s)'
             % (", file" if has_file else "", ",".join("?" * len(chunk))))
        for row in con.execute(q, chunk):
            idx, blob = row[0], row[1]
            if not blob and has_file and row[2]:
                rel = row[2]
                path = rel if os.path.isabs(rel) else os.path.join(base, *rel.split("/"))
                try:
                    with open(path, "rb") as fh:
                        blob = fh.read()
                except OSError:
                    blob = None
            if blob:
                out[idx] = blob
    con.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="信号库或会话库路径")
    ap.add_argument("--from", dest="t0", default=None, help="开始时刻，HH:MM:SS 或完整时间")
    ap.add_argument("--to", dest="t1", default=None, help="结束时刻（不含）")
    ap.add_argument("--index", type=int, default=None, help="只取指定帧号")
    ap.add_argument("--fps", type=float, default=0, help="抽帧频率，0=全取")
    ap.add_argument("--limit", type=int, default=5000, help="最多取多少帧")
    ap.add_argument("--out", default=None, help="输出目录")
    args = ap.parse_args()

    index_db, video_db = resolve_paths(args.db)
    stem = os.path.splitext(os.path.basename(index_db))[0]
    out_dir = args.out or os.path.join(OUT_ROOT, stem)
    os.makedirs(out_dir, exist_ok=True)

    # 只有 HH:MM:SS 时，补上会话日期
    if args.t0 and len(args.t0) <= 8:
        con = sqlite3.connect("file:%s?mode=ro" % index_db.replace("\\", "/"), uri=True)
        tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        table = "camera_index" if "camera_index" in tabs else "camera_frames"
        day = con.execute('select substr(time,1,10) from "%s" limit 1' % table).fetchone()[0]
        con.close()
        args.t0 = "%s %s" % (day, args.t0)
        if args.t1:
            args.t1 = "%s %s" % (day, args.t1)

    if args.index is not None:
        frames = [(None, args.index)]
    else:
        frames = pick_frames(index_db, args.t0, args.t1, args.fps, args.limit)
    if not frames:
        raise SystemExit("该时间段没有帧")

    got = fetch(video_db, [f[1] for f in frames])
    manifest = os.path.join(out_dir, "frames.csv")
    n = 0
    with open(manifest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time", "frame_index", "file", "bytes"])
        for t, idx in frames:
            blob = got.get(idx)
            if blob is None:
                continue
            name = "%s_%06d.jpg" % ((t or "byindex").replace(":", "").replace(" ", "_").replace("-", ""), idx)
            with open(os.path.join(out_dir, name), "wb") as fh:
                fh.write(blob)
            w.writerow([t or "", idx, name, len(blob)])
            n += 1
    print("导出 %d 帧 → %s" % (n, out_dir))
    print("清单：%s" % manifest)
    print("视频源：%s" % video_db)


if __name__ == "__main__":
    main()
