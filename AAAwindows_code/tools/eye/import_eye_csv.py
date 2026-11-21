# -*- coding: utf-8 -*-
"""把独立采集的眼动 CSV 合并进主程序的会话库（eye_data 表）。

两种用法：
    # 1) 给会话目录，自己找同时间段的 CSV 并导入（最省事）
    python tools/eye/import_eye_csv.py --db data/张三/任务A_20261117_171900 --write

    # 2) 明确指定 CSV 与库
    python tools/eye/import_eye_csv.py 眼动数据_7901.csv --db .../20261117_171930.db --write

不给 --db 时挑 data 下最新的会话库（排除 camera_*.db）。
不给 CSV 时在「会话目录 → 上一级 → 项目根 → AAAwindows_code → 当前目录」里找，
按与会话时间窗的重叠时长挑最合适的一个。

三条硬约束（脚本会在报告里检查并提示）：
  1. **同时采集** —— 会话库有自己的时间窗（从开始采集到最后一条数据）。CSV 的
     时间范围必须落在这个窗口内，否则行会被裁掉（--no-clip 可关闭裁剪，但会留下
     时间轴外的孤立数据）。
  2. **同一台机器** —— 时间重建以「CSV 首帧的本机接收时刻」为锚点，换机器时钟差
     未知，整段会整体平移。
  3. **幂等** —— 目标库里该时间窗已有 eye_data 时默认拒绝写入，避免同一文件导两次
     变成双倍数据。确认要追加时显式加 --append。

时间怎么算（默认 --time device）：
  CSV 的「本机时间」列不可直接当 time 列：TCP 是流式的，一次 recv 常拿到十几帧，
  实测 39% 的相邻帧时间戳完全相同、最大空档 217 ms。所以用设备毫秒时间重建：
      time[i] = anchor + (dev_ms[i] - dev_ms[0]) / 1000
  anchor 取首帧的本机接收时刻。这样间隔恢复到设备原始节拍（约 8.5 ms），
  与心电/手环的对齐误差退化为一个基本恒定的网络延迟。
  另提供 --time fit：用全部帧做最小二乘拟合（local = a + b·dev），可抵消设备与
  本机时钟的轻微漂移；--time local：直接用 CSV 的本机时间（不推荐，会抖动）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, 'frozen', False)
        else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
EYE_TABLE = 'eye_data'


def _data_candidates() -> List[str]:
    """可能放着 data 的目录，按优先级。打包后从 exe 所在目录往上找，不能用 __file__。

    每一层都试三种常见布局：<base>\\data、<base>\\AAAwindows_code\\data、
    <base>\\dist\\HcpsMonitor\\data —— 工具包可能放在项目根、程序目录或 dist 里。
    """
    out, seen, base = [], set(), ROOT
    for _ in range(6):
        for tail in (('data',), ('AAAwindows_code', 'data'),
                     ('dist', 'HcpsMonitor', 'data'),
                     ('AAAwindows_code', 'dist', 'HcpsMonitor', 'data')):
            path = os.path.join(base, *tail)
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                out.append(path)
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    out.append(os.path.join(os.getcwd(), 'data'))
    return out


def _has_db(folder: str) -> bool:
    if not os.path.isdir(folder):
        return False
    for _root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in ('frames', 'video', '_frames')]
        if any(n.lower().endswith('.db') and not n.lower().startswith('camera_') for n in files):
            return True
    return False


def _default_data_dir() -> str:
    for path in _data_candidates():
        if _has_db(path):
            return path
    return _data_candidates()[0]


DATA_DIR = _default_data_dir()
# 会话窗口只看这些承载真实数据的表；eye_data 自己要排除，否则导入后窗口会自我扩张
WINDOW_TABLES = ('ecg_data', 'temperature_data', 'breath_data', 'triaxial_data',
                 'bracelet_ppg_data', 'bracelet_accel_data', 'bracelet_gsr_data',
                 'bracelet_skin_temp', 'camera_frames', 'yawn_events')


def to_epoch(text: str) -> Optional[float]:
    """把库里的时间串转成秒；格式不对返回 None（不猜）。"""
    try:
        return datetime.strptime(text.strip(), TIME_FORMAT).timestamp()
    except (ValueError, AttributeError):
        return None


def fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime(TIME_FORMAT)


# ---------------------------------------------------------------------------
# 读 CSV（完整）
# ---------------------------------------------------------------------------
def load_csv(path: str) -> Tuple[List[str], List[Tuple[float, int, Dict[str, object]]]]:
    """返回 (字段名列表, [(本机时刻, 设备毫秒, 原始字段字典)])。"""
    with io.open(path, encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise SystemExit('CSV 是空的：{}'.format(path))
    header = rows[0]
    if len(header) < 3 or '本机时间' not in header[0] or '设备' not in header[1]:
        raise SystemExit('这个 CSV 不是 receive_tcp_5001.py 的格式，前两列应为'
                         '「本机时间」「设备时间(ms)」；实际是 {}'.format(header[:3]))
    keys = header[2:]
    out = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        local = to_epoch(row[0])
        try:
            dev_ms = int(float(row[1]))
        except ValueError:
            continue
        if local is None:
            continue
        fields: Dict[str, object] = {}
        for key, cell in zip(keys, row[2:]):
            if cell == '':
                fields[key] = None
                continue
            try:
                number = float(cell)
            except ValueError:
                fields[key] = cell
                continue
            fields[key] = int(number) if number.is_integer() and '.' not in cell else number
        out.append((local, dev_ms, fields))
    out.sort(key=lambda item: item[1])          # 按设备时间排序（CSV 理论上有序，兜底）
    return keys, out


def csv_span(path: str) -> Optional[Tuple[float, float, int]]:
    """只读首尾，快速估算 (起, 止, 行数)，用于配对；失败返回 None。"""
    try:
        with io.open(path, 'rb') as handle:
            head = handle.readline().decode('utf-8-sig', 'replace')
            if '本机时间' not in head or '设备' not in head:
                return None
            first = handle.readline().decode('utf-8', 'replace')
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return None
    if not first:
        return None
    first_cells = next(csv.reader([first]), [])
    if len(first_cells) < 2:
        return None
    start = to_epoch(first_cells[0])
    if start is None:
        return None
    last_line = ''
    for line in reversed(tail):
        if line.strip() and '本机时间' not in line:
            last_line = line
            break
    end = start
    if last_line:
        cells = next(csv.reader([last_line]), [])
        if len(cells) >= 2:
            end = to_epoch(cells[0]) or start
    # 行数只用于展示，按平均行长估算，避免为了配对把几十 MB 文件读一遍
    lines = max(0, (size // max(len(first) + len(last_line or ''), 1)) // 2)
    return start, max(start, end), lines


# ---------------------------------------------------------------------------
# 找库 / 找 CSV
# ---------------------------------------------------------------------------
def pick_db(explicit: str = '') -> str:
    """--db 可以是 .db 文件，也可以是会话目录（取其中最新的会话库）。"""
    if explicit:
        target = os.path.abspath(explicit)
        if os.path.isdir(target):
            candidates = []
            for name in os.listdir(target):
                if not name.lower().endswith('.db'):
                    continue
                if name.lower().startswith('camera_'):
                    continue
                path = os.path.join(target, name)
                candidates.append((os.path.getmtime(path), path))
            if not candidates:
                raise SystemExit('目录里没有会话库（*.db）：{}'.format(target))
            candidates.sort(reverse=True)
            if len(candidates) > 1:
                print('[导入] 目录里有 {} 个库，取最新的：{}'.format(
                    len(candidates), os.path.basename(candidates[0][1])))
            return candidates[0][1]
        return target

    best, best_mtime = '', 0.0
    for folder, _dirs, files in os.walk(DATA_DIR):
        for name in files:
            if not name.lower().endswith('.db') or name.lower().startswith('camera_'):
                continue
            path = os.path.join(folder, name)
            mtime = os.path.getmtime(path)
            if mtime > best_mtime:
                best, best_mtime = path, mtime
    if not best:
        raise SystemExit('{} 下没有会话库，请用 --db 指定'.format(DATA_DIR))
    return best


def search_dirs(db_path: str) -> List[str]:
    """CSV 的搜索范围：会话目录、上一级、data 根、data 上一层、当前目录。"""
    session_dir = os.path.dirname(os.path.abspath(db_path))
    dirs = [session_dir, os.path.dirname(session_dir), DATA_DIR,
            os.path.dirname(DATA_DIR), os.getcwd()]
    out, seen = [], set()
    for path in dirs:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        out.append(path)
    return out


def find_csv(db_path: str, window: Tuple[float, float], explicit: str = '') -> Optional[str]:
    """在搜索范围内挑与会话时间窗重叠最多的 CSV；没有重叠就返回最接近的那个。"""
    if explicit:
        return os.path.abspath(explicit)
    start, end = window
    best = None                      # (重叠秒, 路径, 跨度)
    closest = None                   # (间隔秒, 路径, 跨度)
    scanned = 0
    for folder in search_dirs(db_path):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith('.csv'):
                continue
            path = os.path.join(folder, name)
            span = csv_span(path)
            if span is None:
                continue
            scanned += 1
            overlap = min(end, span[1]) - max(start, span[0])
            gap = max(span[0] - end, start - span[1], 0.0)
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, path, span)
            if closest is None or gap < closest[0]:
                closest = (gap, path, span)
    if best:
        overlap, path, span = best
        print('[导入] 候选 CSV: 扫了 {} 个，选重叠 {:.0f} 秒的 {}'.format(
            scanned, overlap, os.path.basename(path)))
        return path
    if closest:
        print('[导入] 没有时间重叠的 CSV；最接近的是 {}（相差 {:.0f} 秒，交集为空）'.format(
            os.path.basename(closest[1]), closest[0]))
        return closest[1]
    print('[导入] 搜索范围内没找到眼动 CSV（扫了 {} 个 csv）；请直接指定文件'.format(scanned))
    return None


# ---------------------------------------------------------------------------
# 会话窗口
# ---------------------------------------------------------------------------
def session_window(conn: sqlite3.Connection) -> Tuple[Optional[float], Optional[float], str]:
    """从会话库里算出数据时间窗；返回 (起, 止, 依据表名)。"""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    starts, ends, used = [], [], []
    for table in WINDOW_TABLES:
        if table not in tables:
            continue
        row = conn.execute('SELECT MIN(time), MAX(time) FROM "{}"'.format(table)).fetchone()
        if not row or not row[0]:
            continue
        a, b = to_epoch(row[0]), to_epoch(row[1])
        if a is None:
            continue
        starts.append(a)
        ends.append(b if b is not None else a)
        used.append(table)
    if not starts:
        return None, None, ''
    return min(starts), max(ends), '+'.join(used[:3]) + ('…' if len(used) > 3 else '')


# ---------------------------------------------------------------------------
# 时间重建
# ---------------------------------------------------------------------------
def build_times(rows: Sequence[Tuple[float, int, Dict[str, object]]], mode: str) -> List[float]:
    """按模式生成写库用的时间（epoch 秒）。"""
    if not rows:
        return []
    if mode == 'local':
        return [row[0] for row in rows]

    anchor = rows[0][0]
    base_dev = rows[0][1]
    if mode == 'device':
        return [anchor + (row[1] - base_dev) / 1000.0 for row in rows]

    # fit：最小二乘 local = a + b·dev，抵消采集期间的时钟漂移
    n = len(rows)
    xs = [row[1] / 1000.0 for row in rows]
    ys = [row[0] for row in rows]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if n < 2 or sxx == 0:
        return [anchor + (row[1] - base_dev) / 1000.0 for row in rows]
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    return [a + b * x for x in xs]


# ---------------------------------------------------------------------------
# 行构造
# ---------------------------------------------------------------------------
def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number == -1 else number            # 设备约定 -1 = 无效


def build_value(fields: Dict[str, object], dev_ms: int) -> Dict[str, object]:
    """value 列的 JSON：原始字段全留 + 归一化键 + 来源标记。"""
    gx, gy = _num(fields.get('gaze2dX')), _num(fields.get('gaze2dY'))
    left = fields.get('leftPupil', fields.get('leftPupilDiameter'))
    right = fields.get('rightPupil', fields.get('rightPupilDiameter'))
    raw_valid = fields.get('gaze_valid', fields.get('gazeValid'))
    if isinstance(raw_valid, bool):
        valid = raw_valid
    elif raw_valid is None:
        valid = gx is not None and gy is not None
    else:
        valid = str(raw_valid).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'on')

    value: Dict[str, object] = dict(fields)
    value.update({
        'gaze_valid': valid,
        'gaze_x': gx,
        'gaze_y': gy,
        'left_pupil': _num(left),
        'right_pupil': _num(right),
        'ts_unix': round(dev_ms / 1000.0, 6),
        'ts_raw': str(dev_ms),
        'src': 'csv-import',
    })
    return value


# ---------------------------------------------------------------------------
# 批量查看：哪些会话能配上 CSV
# ---------------------------------------------------------------------------
def list_sessions() -> int:
    if not os.path.isdir(DATA_DIR):
        print('没有 data 目录：{}'.format(DATA_DIR))
        return 2
    print('{:<34} {:<21} {:<21} {:>7} {:>7}  {}'.format(
        '会话库', '会话开始', '会话结束', '重叠秒', '眼动行', 'CSV'))
    print('-' * 118)
    found = 0
    for folder, dirs, files in os.walk(DATA_DIR):
        dirs[:] = [d for d in dirs if d not in ('_frames', 'frames', 'video')]
        for name in files:
            if not name.lower().endswith('.db') or name.lower().startswith('camera_'):
                continue
            path = os.path.join(folder, name)
            try:
                conn = sqlite3.connect('file:{}?mode=ro'.format(path), uri=True)
            except sqlite3.Error:
                continue
            try:
                start, end, _basis = session_window(conn)
                if start is None:
                    continue
                eye_rows = 0
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (EYE_TABLE,)).fetchone():
                    eye_rows = conn.execute('SELECT COUNT(*) FROM "{}"'.format(EYE_TABLE)
                                            ).fetchone()[0]
            except sqlite3.Error:
                conn.close()
                continue
            conn.close()

            csv_path, overlap = '', 0.0
            for base in search_dirs(path):
                try:
                    names = os.listdir(base)
                except OSError:
                    continue
                for csv_name in names:
                    if not csv_name.lower().endswith('.csv'):
                        continue
                    span = csv_span(os.path.join(base, csv_name))
                    if span is None:
                        continue
                    value = min(end, span[1]) - max(start, span[0])
                    if value > overlap:
                        overlap, csv_path = value, os.path.join(base, csv_name)
            rel = os.path.relpath(path, DATA_DIR)
            print('{:<34} {:<21} {:<21} {:>7.0f} {:>7d}  {}'.format(
                rel[-34:], fmt_time(start)[11:19], fmt_time(end)[11:19],
                overlap, eye_rows, os.path.basename(csv_path) or '—'))
            found += 1
    print('-' * 118)
    print('共 {} 个会话。重叠秒 > 0 的才适合导入；眼动行 > 0 说明已经导过了。'.format(found))
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _run(argv=None) -> int:
    ap = argparse.ArgumentParser(description='把眼动 CSV 合并进会话库 eye_data 表')
    ap.add_argument('csv', nargs='?', default='', help='眼动 CSV；不给就按会话时间窗自动找')
    ap.add_argument('--db', default='', help='会话库或会话目录；不给就挑 data 下最新的')
    ap.add_argument('--table', default=EYE_TABLE)
    ap.add_argument('--time', choices=('device', 'fit', 'local'), default='device',
                    help='时间轴口径：device=设备时间重建（默认），fit=最小二乘，local=CSV 本机时间')
    ap.add_argument('--no-clip', action='store_true', help='不按会话时间窗裁剪（默认裁剪）')
    ap.add_argument('--margin', type=float, default=2.0,
                    help='裁剪时前后各留多少秒余量，默认 2.0')
    ap.add_argument('--append', action='store_true',
                    help='目标库该时间窗已有 eye_data 时仍然写入（默认拒绝，防重复导入）')
    ap.add_argument('--no-dedup', action='store_true', help='不去重（默认去掉内容完全相同的相邻帧）')
    ap.add_argument('--write', action='store_true', help='真正写库；不加只做 dry-run')
    ap.add_argument('-i', '--interactive', action='store_true',
                    help='报告后询问一次是否写入（打包 exe 双击时用）')
    ap.add_argument('--list', action='store_true', help='列出所有会话与可配对的 CSV 后退出')
    args = ap.parse_args(argv)

    if args.list:
        return list_sessions()

    db = pick_db(args.db)
    if not os.path.exists(db):
        print('[导入] 目标库不存在：{}'.format(db))
        return 2
    print('[导入] 目标库 : {}'.format(db))

    conn = sqlite3.connect(db, timeout=10)
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        start, end, basis = session_window(conn)
        if start is None:
            print('[导入] 这个库里没有任何数据表，无法确定会话时间窗 —— 拒绝导入')
            print('        （空库多半是程序刚启动还没开始采集）')
            return 3
        print('[导入] 会话窗 : {} ~ {}（依据 {}）'.format(
            fmt_time(start), fmt_time(end), basis))

        csv_path = find_csv(db, (start, end), args.csv)
        if not csv_path or not os.path.exists(csv_path):
            print('[导入] 没找到可用的 CSV，请把文件路径作为参数传进来')
            return 2
        print('[导入] CSV    : {}'.format(csv_path))

        keys, rows = load_csv(csv_path)
        if not rows:
            print('[导入] CSV 里没有可用行')
            return 2
        print('[导入] 字段数 : {}｜数据行 : {}'.format(len(keys), len(rows)))

        times = build_times(rows, args.time)
        deltas = sorted(abs(t - row[0]) for t, row in zip(times, rows))
        mid = deltas[len(deltas) // 2]
        p95 = deltas[int(len(deltas) * 0.95)] if len(deltas) > 1 else mid
        print('[导入] 时间口径: {}｜与 CSV 本机时间偏差 中位 {:.0f} ms / P95 {:.0f} ms'.format(
            args.time, mid * 1000, p95 * 1000))

        lo, hi = start - args.margin, end + args.margin
        keep = [(t, row) for t, row in zip(times, rows) if lo <= t <= hi] \
            if not args.no_clip else list(zip(times, rows))
        dropped = len(rows) - len(keep)
        print('[导入] 窗口内 : {} 行{}'.format(
            len(keep), '｜裁掉 {} 行（在会话时间窗之外）'.format(dropped) if dropped else ''))

        if not args.no_dedup:
            uniq, seen_prev = [], None
            for t, row in keep:
                key = json.dumps(build_value(row[2], row[1]), sort_keys=True, ensure_ascii=False)
                if key == seen_prev:
                    continue
                uniq.append((t, row))
                seen_prev = key
            dup = len(keep) - len(uniq)
            keep = uniq
            if dup:
                print('[导入] 去重  : 去掉 {} 行完全相同的帧'.format(dup))

        if not keep:
            print('[导入] 没有可写入的行：CSV 的时间范围与会话窗不重叠')
            print('        多半是两边没有同时采集 —— 眼动要覆盖主程序那次采集的时间段')
            return 4

        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        existing = 0
        if args.table in tables:
            existing = conn.execute(
                'SELECT COUNT(*) FROM "{}" WHERE time BETWEEN ? AND ?'.format(args.table),
                (fmt_time(lo), fmt_time(hi))).fetchone()[0]
        if existing and not args.append:
            print('[导入] 目标窗内已有 {} 行 eye_data —— 拒绝写入（防重复导入）'.format(existing))
            print('        确认要追加就加 --append；想重来先用 SQL 删掉那一段')
            return 5
        if existing:
            print('[导入] 注意  : 目标窗内已有 {} 行，按 --append 继续追加'.format(existing))

        payload = [(fmt_time(t), json.dumps(build_value(row[2], row[1]),
                                            ensure_ascii=False)) for t, row in keep]
        print('[导入] 写入范围: {} ~ {}'.format(payload[0][0][:23], payload[-1][0][:23]))
        print('[导入] 时长   : {:.1f} 秒｜平均 {:.0f} 帧/秒'.format(
            keep[-1][0] - keep[0][0], len(keep) / max(keep[-1][0] - keep[0][0], 1e-6)))

        if not args.write:
            if not (args.interactive and sys.stdin and sys.stdin.isatty()):
                print('[导入] DRY-RUN：没有写库。确认无误后加 --write 执行')
                return 0
            try:
                answer = input('[导入] 确认写入上面这个库？(y/N) ').strip().lower()
            except EOFError:
                answer = ''
            if answer not in ('y', 'yes'):
                print('[导入] 已取消，未写入')
                return 0
            args.write = True

        conn.execute('CREATE TABLE IF NOT EXISTS "{}" (time TEXT, value TEXT)'.format(args.table))
        conn.execute('BEGIN')
        conn.executemany('INSERT INTO "{}" (time, value) VALUES (?, ?)'.format(args.table),
                         payload)
        conn.commit()
        total = conn.execute('SELECT COUNT(*) FROM "{}"'.format(args.table)).fetchone()[0]
        print('[导入] 已写入 {} 行｜该表现有 {} 行'.format(len(payload), total))
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    """入口包装：打包成 exe 后双击运行，结束前停一下，否则窗口一闪而过看不到报告。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _run(argv)
    finally:
        if getattr(sys, 'frozen', False) and not argv and sys.stdin and sys.stdin.isatty():
            try:
                input('\n按回车退出…')
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == '__main__':
    sys.exit(main())
