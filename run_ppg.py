# -*- coding: utf-8 -*-
"""跑 PPG 融合管线（ppg_pipeline）的入口。

用法：
    python run_ppg.py                          # 用 data/ 下最新的会话库
    python run_ppg.py --db <会话.db>            # 指定库
    python run_ppg.py --db <会话.db> --out <目录>

默认输出到 ppg_analysis/<会话目录名>/output/。
必须用 shifeiyuantwo 这个 venv 的 python（需要 scipy / matplotlib）。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(ROOT, "ppg_pipeline")


def latest_db() -> str:
    hits = glob.glob(os.path.join(ROOT, "AAAwindows_code", "data", "**", "*.db"),
                     recursive=True)
    if not hits:
        raise SystemExit("没找到任何会话库：AAAwindows_code/data/**/*.db")
    return max(hits, key=os.path.getmtime)


def main() -> int:
    ap = argparse.ArgumentParser(description="绿光手环四通道 PPG 融合分析")
    ap.add_argument("--db", help="SQLite 会话库路径（默认取 data/ 下最新的）")
    ap.add_argument("--out", help="输出目录（默认 ppg_analysis/<会话名>/output）")
    args = ap.parse_args()

    db = os.path.abspath(args.db) if args.db else latest_db()
    if not os.path.exists(db):
        raise SystemExit("库不存在：{}".format(db))

    session = os.path.basename(os.path.dirname(db)) or "session"
    out = os.path.abspath(args.out) if args.out else os.path.join(
        ROOT, "ppg_analysis", session, "output")

    os.environ["PPG_DB"] = db
    os.environ["PPG_OUT"] = out
    print("数据库 :", db)
    print("输出目录:", out)

    sys.path.insert(0, PKG)
    os.chdir(PKG)                       # run_pipeline 用的是平铺 import
    import run_pipeline                  # noqa: E402  - 必须改完 cwd/sys.path 再导
    run_pipeline.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
