# -*- coding: utf-8 -*-
"""把「心算任务」的 CSV 结果导入成一个会话库，放进程序的数据目录。

产物结构（与采集程序的会话目录一致，回放/分析页能直接看到）：

    AAAwindows_code/data/心算/心算任务_<日期>/心算任务_<日期>.db
                                          /心算任务数据_<时间>.csv   ← 原始文件留档

库里的表：
    meta(key, value)                     来源、导入时间、汇总
    runs(run_id, source, started_at, …… ) 每次跑一遍算一个 run
    trials(run_id, trial, elapsed_s, rt_ms, correct, miss)

用法：
    python 导入心算数据.py                       # 自动找桌面\\临时文件夹里的 心算任务数据_*.csv
    python 导入心算数据.py --src <目录或csv>      # 指定来源
    python 导入心算数据.py --name 心算测试        # 自定义会话名（默认「心算任务_<日期>」）
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'AAAwindows_code', 'data')
DEFAULT_SRC = os.path.join(os.path.expanduser('~'), 'Desktop', '临时文件夹')

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY,
    source      TEXT,
    started_at  TEXT,
    n_trials    INTEGER,
    n_correct   INTEGER,
    n_miss      INTEGER,
    mean_rt_ms  REAL,
    median_rt_ms REAL
);
CREATE TABLE IF NOT EXISTS trials (
    run_id   INTEGER NOT NULL,
    trial    INTEGER NOT NULL,
    elapsed_s REAL,
    rt_ms    REAL,
    correct  INTEGER,
    miss     INTEGER,
    PRIMARY KEY (run_id, trial)
);
CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(run_id);
"""


def _stamp_from_name(path: str) -> str:
    """心算任务数据_20261105_223549.csv → 2026-11-05 22:35:49"""
    stem = os.path.splitext(os.path.basename(path))[0]
    part = stem.split('_')[-2:]
    try:
        return datetime.strptime('_'.join(part), '%Y%m%d_%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')


def _read_csv(path: str) -> list:
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _pick_truthy(text) -> int:
    return 1 if str(text).strip().lower() in ('true', '1', 'yes', 'y', '是') else 0


def collect_sources(src: str) -> list:
    if os.path.isfile(src):
        return [src]
    found = sorted(glob.glob(os.path.join(src, '心算任务数据_*.csv')))
    if not found:
        found = sorted(glob.glob(os.path.join(src, '*心算*.csv')))
    return found


def build(sources: list, out_dir: str, db_name: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, db_name)

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute('DELETE FROM trials')
    con.execute('DELETE FROM runs')

    total_trials = 0
    for run_id, path in enumerate(sources, start=1):
        rows = _read_csv(path)
        started = _stamp_from_name(path)
        trials = []
        for index, row in enumerate(rows, start=1):
            trials.append((
                run_id,
                int(_num(row.get('trial')) or index),
                _num(row.get('elapsed_s')),
                _num(row.get('rt_ms')),
                _pick_truthy(row.get('correct')),
                _pick_truthy(row.get('miss')),
            ))
        con.executemany(
            'INSERT OR REPLACE INTO trials (run_id, trial, elapsed_s, rt_ms, correct, miss) '
            'VALUES (?, ?, ?, ?, ?, ?)', trials)

        rts = sorted(t[3] for t in trials if t[3] is not None and not t[5])
        n_correct = sum(t[4] for t in trials)
        n_miss = sum(t[5] for t in trials)
        mean_rt = round(sum(rts) / len(rts), 1) if rts else None
        median_rt = round(rts[len(rts) // 2], 1) if rts else None
        con.execute(
            'INSERT INTO runs (run_id, source, started_at, n_trials, n_correct, n_miss, '
            'mean_rt_ms, median_rt_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (run_id, os.path.basename(path), started, len(trials), n_correct, n_miss,
             mean_rt, median_rt))
        total_trials += len(trials)

        # 原始 CSV 一起留档，方便回溯
        target_csv = os.path.join(out_dir, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(target_csv):
            with open(path, 'rb') as src_handle, open(target_csv, 'wb') as dst_handle:
                dst_handle.write(src_handle.read())

    summary = dict(sources=[os.path.basename(p) for p in sources],
                   n_runs=len(sources), n_trials=total_trials,
                   imported_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    con.executemany('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
                    [('summary', json.dumps(summary, ensure_ascii=False)),
                     ('task', '心算任务'),
                     ('imported_at', summary['imported_at'])])
    con.commit()
    con.close()
    return db_path


def main() -> int:
    ap = argparse.ArgumentParser(description='把心算任务 CSV 导入成会话库')
    ap.add_argument('--src', default=DEFAULT_SRC, help='CSV 文件或所在目录')
    ap.add_argument('--name', default=None, help='会话名（默认 心算任务_<日期>）')
    args = ap.parse_args()

    sources = collect_sources(args.src)
    if not sources:
        raise SystemExit('没找到心算任务数据：{}'.format(args.src))

    first_day = _stamp_from_name(sources[0])[:10].replace('-', '')
    name = args.name or '心算任务_{}'.format(first_day)
    out_dir = os.path.join(DATA_DIR, '心算', name)
    db_path = build(sources, out_dir, name + '.db')

    print('来源 {} 个 CSV：'.format(len(sources)))
    for path in sources:
        print('   ', path)
    print('\n导入到：', db_path)

    con = sqlite3.connect(db_path)
    print('\n{' + '-' * 62)
    print('%-4s %-26s %6s %8s %10s' % ('run', '开始时间', '题数', '正确', '平均RT(ms)'))
    for row in con.execute('SELECT run_id, started_at, n_trials, n_correct, mean_rt_ms '
                           'FROM runs ORDER BY run_id'):
        print('%-4d %-26s %6d %8d %10s' % (row[0], row[1], row[2], row[3], row[4]))
    total = con.execute('SELECT COUNT(*) FROM trials').fetchone()[0]
    print('-' * 62)
    print('合计 {} 次运行 / {} 道题'.format(len(sources), total))
    con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
