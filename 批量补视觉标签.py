# -*- coding: utf-8 -*-
"""批量给 9 个历史会话库补视觉标签（逐帧全跑，GPU）。

调度策略
--------
* 一个库一个进程，**同一个库永远只被一个进程写**，避免 SQLite 写锁打架。
* 按帧数从多到少调度（LPT），先把最长的扔进去，缩短总完成时间。
* 实测：单进程 GPU 约 38 fps，两进程合计 47 fps（CPU 端解码是瓶颈），
  所以默认并发 3，再多收益很小。
* 每个库用 `--resume`：中断后重跑只补没算的部分，不会重复写。

用法
----
    python 批量补视觉标签.py                # 默认 3 并发
    python 批量补视觉标签.py --workers 2
    python 批量补视觉标签.py --dry-run
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "AAAwindows_code")
PY = os.path.join(ROOT, "shifeiyuantwo", "Scripts", "python.exe")
LOG = os.path.join(ROOT, "视觉回填日志.txt")


def log(msg: str):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def inventory():
    """返回 [(路径, 帧数, 已有秒级行数)]，按帧数降序。"""
    items = []
    for p in glob.glob(os.path.join(APP, "data", "**", "*.db"), recursive=True):
        if os.path.getsize(p) < 1048576:
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
            tabs = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "camera_frames" not in tabs:
                con.close()
                continue
            frames = con.execute("SELECT COUNT(*) FROM camera_frames").fetchone()[0]
            done = 0
            if "vision_frames" in tabs:
                done = con.execute("SELECT COUNT(*) FROM vision_frames").fetchone()[0]
            con.close()
        except sqlite3.Error:
            continue
        if frames:
            items.append((p, frames, done))
    items.sort(key=lambda x: -x[1])
    return items


def run_one(path: str, frames: int, dry_run: bool, logf):
    cmd = [PY, os.path.join(ROOT, "补视觉标签.py"), "--db", path,
           "--fps", "0", "--device", "gpu", "--resume"]
    if dry_run:
        cmd.append("--dry-run")
    started = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="ignore",
                         cwd=ROOT)
    last = ""
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        if re.search(r"\d+/\d+", line):
            last = line.strip()
            with open(LOG, "a", encoding="utf-8") as f:
                f.write("    %s | %s\n" % (os.path.basename(os.path.dirname(
                    os.path.dirname(path)))[:16], line.strip()))
        elif "完成" in line or "失败" in line:
            log("  %s → %s" % (os.path.basename(os.path.dirname(
                os.path.dirname(path))), line.strip()))
    p.wait()
    return p.returncode, time.time() - started


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, help="只跑名字里含该字符串的库")
    args = ap.parse_args()

    items = inventory()
    if args.only:
        items = [i for i in items if args.only in i[0]]
    total_frames = sum(i[1] for i in items)
    log("=" * 60)
    log("待处理 %d 个库，共 %d 帧（逐帧全跑），并行 %d"
        % (len(items), total_frames, args.workers))
    for p, n, d in items:
        log("  %-30s %7d 帧  已有 %d 行"
            % (os.path.basename(os.path.dirname(os.path.dirname(p)))[:30], n, d))

    queue = list(items)
    lock = threading.Lock()
    done_frames = [0]
    started = time.time()

    def worker(idx: int):
        while True:
            with lock:
                if not queue:
                    return
                path, frames, _d = queue.pop(0)
            name = os.path.basename(os.path.dirname(os.path.dirname(path)))[:24]
            log("[W%d] 开始 %s（%d 帧）" % (idx, name, frames))
            code, el = run_one(path, frames, args.dry_run, None)
            with lock:
                if code == 0:
                    done_frames[0] += frames
            rate = frames / max(el, 1e-6)
            log("[W%d] %s 结束 code=%d %.0f 分（%.1f fps）"
                % (idx, name, code, el / 60.0, rate))

    threads = [threading.Thread(target=worker, args=(i + 1,), daemon=True)
               for i in range(min(args.workers, max(1, len(items))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    el = time.time() - started
    log("全部结束：%d 帧 / %.1f 小时（平均 %.1f fps）"
        % (done_frames[0], el / 3600.0, done_frames[0] / max(el, 1e-6)))


if __name__ == "__main__":
    main()
