# -*- coding: utf-8 -*-
"""把老会话库里存的摄像头 JPEG 搬到文件夹，库里只留索引。

为什么
    2026-11 之前摄像头帧是以 BLOB 存在会话库 camera_frames 里的，一场 100 分钟
    的会话能到 3~9 GB，其中 96% 是图片。翻页、查询、拷贝、备份都得拖着整个库走。

做法
    <会话库所在目录>/camera_<时间戳>/frames/000001.jpg …
    库里 camera_frames 的 jpeg 置空、file 写成上面的相对路径。
    之后回放、取帧、分析都按 file 读文件，库本身降到几 MB。

用法
    python 抽帧到文件夹.py                 # 干跑：只统计，不动任何文件
    python 抽帧到文件夹.py --apply         # 真的搬（会先备份一份索引 CSV）
    python 抽帧到文件夹.py --apply --vacuum  # 搬完顺手 VACUUM，把腾出的空间还给磁盘
    python 抽帧到文件夹.py --only 高晨瑞 --apply
    python 抽帧到文件夹.py 某个.db --apply

注意
    · 默认是干跑，不加 --apply 不会碰任何东西；
    · 已经是新格式（jpeg 为空、file 有值）的库会自动跳过；
    · VACUUM 会重写整个库文件，需要该盘有与库同样大的空闲空间；
    · 搬之前请确认这些库没有被采集程序打开。
"""
import argparse
import csv
import glob
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "AAAwindows_code", "data")
FRAMES_PER_DIR = 1000          # 与 devices/camera_receiver.py 的 frame_rel 一致


def frame_rel(index: int) -> str:
    index = int(index)
    return "{:04d}/{:06d}.jpg".format(index // FRAMES_PER_DIR, index)


def find_dbs(pattern=None):
    if pattern:
        return [os.path.abspath(pattern)]
    out = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "**", "*.db"), recursive=True)):
        if os.path.getsize(path) < 1024 * 1024:
            continue
        out.append(path)
    return out


def scan(path):
    """返回 (帧数, 已搬走的帧数, 图片字节数, 首个时间, 是否含 file 列)。"""
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
        if "camera_frames" not in tables:
            return None
        cols = {r[1] for r in con.execute('PRAGMA table_info("camera_frames")')}
        has_file = "file" in cols
        total, done, blob_bytes = con.execute(
            'select count(*),'
            ' sum(case when jpeg is null or length(jpeg)=0 then 1 else 0 end),'
            ' sum(length(jpeg)) from camera_frames').fetchone()
        first = con.execute(
            'select min(time) from camera_frames').fetchone()[0]
        return dict(total=total or 0, done=done or 0, bytes=blob_bytes or 0,
                    first=first, has_file=has_file, cols=cols)
    finally:
        con.close()


def session_dir_name(first_time, db_path):
    """图片目录名：优先用首帧时间，退成库文件名。"""
    stamp = ""
    if first_time:
        try:
            from datetime import datetime
            stamp = datetime.strptime(first_time[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y%m%d_%H%M%S")
        except ValueError:
            stamp = ""
    if not stamp:
        stamp = os.path.splitext(os.path.basename(db_path))[0].replace("camera_", "")
    return "camera_" + stamp


def migrate(path, apply=False, vacuum=False, keep_blob=False):
    info = scan(path)
    if info is None:
        return None
    if info["total"] == 0:
        return None
    if info["has_file"] and info["bytes"] == 0:
        return dict(path=path, skipped=True, total=info["total"])
    if info["bytes"] == 0:
        return dict(path=path, skipped=True, total=info["total"])

    base = os.path.dirname(os.path.abspath(path))
    folder = session_dir_name(info["first"], path)
    frames_dir = os.path.join(base, folder, "frames")
    rel_prefix = folder + "/frames"
    size_mb = info["bytes"] / 1048576.0

    print("  %s：%d 帧、图片 %.1f MB → %s"
          % (os.path.basename(path), info["total"], size_mb, frames_dir), flush=True)
    if not apply:
        return dict(path=path, total=info["total"], bytes=info["bytes"],
                    frames_dir=frames_dir, applied=False)

    os.makedirs(frames_dir, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        if "file" not in info["cols"]:
            con.execute("ALTER TABLE camera_frames ADD COLUMN file TEXT")
        rows = con.execute(
            "select id, frame_index, jpeg from camera_frames "
            "where jpeg is not null and length(jpeg)>0 order by id").fetchall()
        csv_path = os.path.join(base, folder + "_帧清单.csv")
        written = 0
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "frame_index", "file", "bytes"])
            updates = []
            used = set()
            for rowid, index, blob in rows:
                rel_name = frame_rel(index if index is not None else rowid)
                if rel_name in used:                 # 帧号重复（重启过采集）时加后缀
                    rel_name = rel_name.replace(".jpg", "_{}.jpg".format(rowid))
                used.add(rel_name)
                target = os.path.join(frames_dir, *rel_name.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as img:
                    img.write(blob)
                rel = rel_prefix + "/" + rel_name
                writer.writerow([rowid, index, rel, len(blob)])
                updates.append((rel, b"" if not keep_blob else blob, rowid))
                written += 1
                if len(updates) >= 500:
                    con.executemany("UPDATE camera_frames SET file=?, jpeg=? WHERE id=?",
                                    updates)
                    updates.clear()
            if updates:
                con.executemany("UPDATE camera_frames SET file=?, jpeg=? WHERE id=?", updates)
        con.commit()
        print("    写入 %d 张图，清单 %s" % (written, os.path.basename(csv_path)), flush=True)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    if vacuum:
        before = os.path.getsize(path)
        con = sqlite3.connect(path)
        con.execute("VACUUM")
        con.close()
        after = os.path.getsize(path)
        print("    VACUUM：%.1f MB → %.1f MB" % (before / 1048576, after / 1048576), flush=True)

    return dict(path=path, total=len(rows), bytes=info["bytes"],
                frames_dir=frames_dir, applied=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default=None, help="只处理这一个库")
    ap.add_argument("--only", default=None, help="只处理路径里含该字符串的库")
    ap.add_argument("--apply", action="store_true", help="真的搬（不加只统计）")
    ap.add_argument("--vacuum", action="store_true", help="搬完 VACUUM 回收空间")
    ap.add_argument("--keep-blob", action="store_true",
                    help="只写 file、保留 BLOB（占双份空间，排查问题时用）")
    args = ap.parse_args()

    dbs = find_dbs(args.db)
    if args.only:
        dbs = [p for p in dbs if args.only in p]
    print("%s：待检查 %d 个库" % ("执行" if args.apply else "干跑（加 --apply 才动文件）", len(dbs)))

    t0 = time.time()
    results = []
    for path in dbs:
        try:
            got = migrate(path, apply=args.apply, vacuum=args.vacuum,
                          keep_blob=args.keep_blob)
        except Exception as exc:                                  # noqa: BLE001
            print("  %s：失败 %s" % (os.path.basename(path), exc), flush=True)
            continue
        if got:
            results.append(got)

    if not results:
        print("没有需要处理的库（都是新格式，或库里没有帧）")
        return
    moved = [r for r in results if r.get("applied")]
    skip = [r for r in results if r.get("skipped")]
    total_mb = sum(r.get("bytes", 0) for r in results) / 1048576.0
    print("\n合计 %d 个库、图片 %.1f MB；本次处理 %d 个，跳过 %d 个，用时 %.1f 秒"
          % (len(results), total_mb, len(moved), len(skip), time.time() - t0))
    if not args.apply:
        print("这是干跑。确认无误后加 --apply 执行；要回收库文件体积再加 --vacuum。")


if __name__ == "__main__":
    main()
