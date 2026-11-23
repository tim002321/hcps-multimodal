# -*- coding: utf-8 -*-
"""把云端拉下来的心算会话 JSON 导入成会话库（含逐题明细）。

云端取数：`pull_cloud.js` 调用官方 SDK 的 `pull_sessions` 口令函数，落成
`mental_math_sessions.json`（一次会话一条，逐题明细在 records 里）。

用法：
    python 导入心算云端数据.py                       # 读 .workbuddy/tmp/mental_math_sessions.json
    python 导入心算云端数据.py --json <路径> --name <会话名>

产物（数据目录下，回放/分析页能直接看到）：
    AAAwindows_code/data/心算/<会话名>/
        <会话名>.db           sessions + trials 两张表
        心算汇总.csv           一次会话一行（进 SPSS / R）
        心算逐题明细.csv       长表，每题一行
        云端原始.json          原样留档
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'AAAwindows_code', 'data')
DEFAULT_JSON = os.path.join(ROOT, '.workbuddy', 'tmp', 'mental_math_sessions.json')

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY,
    subject       TEXT,
    person        TEXT,
    group_name    TEXT,
    round_name    TEXT,
    started_at    TEXT,
    duration_min  REAL,
    n_trials      INTEGER,
    accuracy      REAL,
    errors        INTEGER,
    misses        INTEGER,
    rt_mean_ms    REAL,
    rt_median_ms  REAL,
    rt_sd_ms      REAL,
    rt_slowest10_ms REAL,
    speed_1rt     REAL,
    lapses        INTEGER,
    lapse_rate    REAL,
    rt_change_pct REAL,
    client        TEXT,
    created_at    TEXT,
    suspect       INTEGER
);
CREATE TABLE IF NOT EXISTS trials (
    session_id INTEGER NOT NULL,
    trial      INTEGER NOT NULL,
    rt_ms      REAL,
    elapsed_s  REAL,
    correct    INTEGER,
    miss       INTEGER,
    PRIMARY KEY (session_id, trial)
);
CREATE INDEX IF NOT EXISTS idx_trials_session ON trials(session_id);
"""

_CN_NUM = '一二三四五六七八九十'

# 「马国庆第一组实验第四组」这种写法：人 + 第X组 + 实验第Y组
_SUBJECT_RE = re.compile(
    r'^(?P<person>.*?)第(?P<group>[{0}]+)组(?:实验)?第(?P<round>[{0}]+)组$'.format(_CN_NUM))


def split_subject(text: str):
    """把被试编号拆成 (人, 组别, 轮次)，拆不动就原样留在 person。"""
    raw = (text or '').strip()
    match = _SUBJECT_RE.match(raw)
    if not match:
        return raw, '', ''
    return (match.group('person') or raw,
            '第{}组'.format(match.group('group')),
            '第{}组'.format(match.group('round')))


def _truthy(value) -> int:
    return 1 if value is True or str(value).strip().lower() in ('true', '1', 'yes') else 0


def build(sessions: list, out_dir: str, db_name: str, raw_path: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, db_name)

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    # 老库补列（CREATE TABLE IF NOT EXISTS 不会改动已存在的表）
    existing = {row[1] for row in con.execute('PRAGMA table_info(sessions)')}
    if existing and 'suspect' not in existing:
        con.execute('ALTER TABLE sessions ADD COLUMN suspect INTEGER')
    con.execute('DELETE FROM trials')
    con.execute('DELETE FROM sessions')

    summary_rows = []
    detail_rows = []
    for item in sessions:
        sid = int(item.get('id') or 0)
        accuracy = item.get('accuracy')
        n_trials = item.get('n_trials') or 0
        lapse_rate = item.get('lapse_rate') or 0
        # 可疑标记：正确率过低 / 题数太少 / lapse 太多（与看板的红标口径一致）
        suspect = int((accuracy is not None and accuracy < 0.6)
                      or n_trials < 10 or lapse_rate > 0.3)
        subject = (item.get('subject') or '').strip()
        person, group_name, round_name = split_subject(subject)
        con.execute(
            'INSERT OR REPLACE INTO sessions (id, subject, person, group_name, round_name, '
            'started_at, duration_min, n_trials, accuracy, errors, misses, rt_mean_ms, '
            'rt_median_ms, rt_sd_ms, rt_slowest10_ms, speed_1rt, lapses, lapse_rate, '
            'rt_change_pct, client, created_at, suspect) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (sid, subject, person, group_name, round_name,
             item.get('started_at'), item.get('duration_min'), item.get('n_trials'),
             item.get('accuracy'), item.get('errors'), item.get('misses'),
             item.get('rt_mean_ms'), item.get('rt_median_ms'), item.get('rt_sd_ms'),
             item.get('rt_slowest10_ms'), item.get('speed_1rt'), item.get('lapses'),
             item.get('lapse_rate'), item.get('rt_change_pct'),
             (item.get('client') or '')[:200], item.get('created_at'), suspect))

        def rnd(value, digits=1):
            try:
                return round(float(value), digits)
            except (TypeError, ValueError):
                return None

        summary_rows.append([sid, subject, person, group_name, round_name,
                             item.get('started_at'), item.get('duration_min'),
                             n_trials,
                             rnd(100.0 * accuracy) if accuracy is not None else None,
                             item.get('errors'), item.get('misses'),
                             rnd(item.get('rt_mean_ms')), rnd(item.get('rt_median_ms')),
                             rnd(item.get('rt_sd_ms')), rnd(item.get('rt_slowest10_ms')),
                             rnd(item.get('speed_1rt'), 4), item.get('lapses'),
                             rnd(100.0 * lapse_rate) if lapse_rate is not None else None,
                             rnd(item.get('rt_change_pct')),
                             '是' if suspect else ''])

        for record in (item.get('records') or []):
            con.execute(
                'INSERT OR REPLACE INTO trials (session_id, trial, rt_ms, elapsed_s, '
                'correct, miss) VALUES (?,?,?,?,?,?)',
                (sid, int(record.get('trial') or 0), record.get('rt_ms'),
                 record.get('elapsed_s'), _truthy(record.get('correct')),
                 _truthy(record.get('miss'))))
            detail_rows.append([sid, subject, int(record.get('trial') or 0),
                                record.get('rt_ms'), record.get('elapsed_s'),
                                _truthy(record.get('correct')), _truthy(record.get('miss'))])

    total_trials = con.execute('SELECT COUNT(*) FROM trials').fetchone()[0]
    subjects = con.execute('SELECT COUNT(DISTINCT subject) FROM sessions').fetchone()[0]
    con.executemany('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', [
        ('task', '心算任务'),
        ('source', 'cloud:pull_sessions'),
        ('imported_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ('n_sessions', str(len(sessions))),
        ('n_subjects', str(subjects)),
        ('n_trials', str(total_trials)),
    ])
    con.commit()
    con.close()

    # 与取数页三个导出按钮对应的两份 CSV
    with open(os.path.join(out_dir, '心算汇总.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['id', '被试编号', '人员', '组别', '轮次', '开始时间', '时长(分)', '题数',
                    '正确率(%)', '错误数', '漏答数', 'RT均值(ms)', 'RT中位(ms)',
                    'RT标准差(ms)', '最慢10%(ms)', '首题速度(1/RT)', 'lapse数',
                    'lapse率(%)', 'RT变化(%)', '可疑'])
        w.writerows(summary_rows)
    with open(os.path.join(out_dir, '心算逐题明细.csv'), 'w', newline='',
              encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['session', '被试编号', 'trial', 'rt_ms', 'elapsed_s', 'correct', 'miss'])
        w.writerows(detail_rows)

    if raw_path and os.path.exists(raw_path):
        target = os.path.join(out_dir, '云端原始.json')
        with open(raw_path, 'rb') as src, open(target, 'wb') as dst:
            dst.write(src.read())
    return db_path


def main() -> int:
    ap = argparse.ArgumentParser(description='把云端心算会话 JSON 导入成会话库')
    ap.add_argument('--json', default=DEFAULT_JSON, help='pull_cloud.js 拉下来的 JSON')
    ap.add_argument('--name', default=None, help='会话名（默认 心算_云端<会话数>次_<日期>）')
    args = ap.parse_args()

    if not os.path.exists(args.json):
        raise SystemExit('没找到云端数据：{}（先跑 pull_cloud.js）'.format(args.json))
    sessions = json.load(open(args.json, encoding='utf-8'))
    if not sessions:
        raise SystemExit('JSON 里没有会话')

    day = datetime.now().strftime('%Y%m%d')
    name = args.name or '心算_云端{}次_{}'.format(len(sessions), day)
    out_dir = os.path.join(DATA_DIR, '心算', name)
    db_path = build(sessions, out_dir, name + '.db', args.json)

    print('导入 {} 次会话 → {}'.format(len(sessions), db_path))
    con = sqlite3.connect(db_path)
    print('  被试 {} 个 / 会话 {} 次 / 逐题 {} 条'.format(
        con.execute('SELECT COUNT(DISTINCT subject) FROM sessions').fetchone()[0],
        con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
        con.execute('SELECT COUNT(*) FROM trials').fetchone()[0]))
    print('  平均正确率 {:.1f}% / 中位 RT 均值 {:.0f} ms'.format(
        100 * con.execute('SELECT AVG(accuracy) FROM sessions').fetchone()[0],
        con.execute('SELECT AVG(rt_median_ms) FROM sessions').fetchone()[0]))
    con.close()
    print('  同目录还有：心算汇总.csv / 心算逐题明细.csv / 云端原始.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
