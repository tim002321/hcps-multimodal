#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空库回归测试：确认「没采到数据就不留文件」以及会话列表的显示口径。

背景：`SensorDB.__init__` 里一旦 `sqlite3.connect()` + `PRAGMA journal_mode=WAL`，
磁盘上就会留下 4096 字节、零张表的空库；程序每次启动都建一个，没数据就退出，
会话列表里于是堆满「0.0 MB」的条目。

运行：
    .venv\\Scripts\\python.exe tools/db/test_empty_db.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from core.db_storage import EMPTY_DB_BYTES, SensorDB, db_total_size, is_empty_db  # noqa: E402
from ui.replay import format_size, list_sessions  # noqa: E402

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = '') -> None:
    if condition:
        PASSED.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAILED.append(name)
        print('  [FAIL] {} {}'.format(name, detail))


def table_count(path: str) -> int:
    conn = sqlite3.connect('file:{}?mode=ro'.format(path), uri=True)
    try:
        return len([r for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")])
    finally:
        conn.close()


def test_no_file_before_first_write(tmp: str) -> None:
    print('\n[1] 构造 SensorDB 不应在磁盘上留文件')
    db = SensorDB(data_dir=tmp, db_name='session.db')
    check('构造后文件不存在', not os.path.exists(db.db_path), db.db_path)
    check('created 为 False', db.created is False)
    db.close()
    check('close 之后仍然没有文件', not os.path.exists(db.db_path))
    check('目录里没多出任何文件', os.listdir(tmp) == [], str(os.listdir(tmp)))


def test_file_created_on_first_write(tmp: str) -> None:
    print('\n[2] 首次写入才建库，且数据可读')
    db = SensorDB(data_dir=tmp, db_name='written.db')
    db.insert_many('ecg_data', [1.0, 2.0, 3.0])     # 三个采样点，一个时间戳
    db.close()
    check('写入后文件存在', os.path.exists(db.db_path))
    check('不是空库', not is_empty_db(db.db_path))
    check('有 1 张表', table_count(db.db_path) == 1, '实际 {}'.format(table_count(db.db_path)))
    conn = sqlite3.connect('file:{}?mode=ro'.format(db.db_path), uri=True)
    rows = conn.execute('SELECT COUNT(*) FROM ecg_data').fetchone()[0]
    conn.close()
    check('3 个采样点入库', rows == 3, '实际 {}'.format(rows))


def test_session_subdir_lazy(tmp: str) -> None:
    print('\n[3] 分层会话目录也只在写入时才创建')
    db = SensorDB(data_dir=tmp, session_subdir='A组/张三_20261030_120000')
    session_dir = os.path.dirname(db.db_path)
    check('目录未创建', not os.path.exists(session_dir), session_dir)
    db.insert('temperature_data', 36.5)
    db.close()
    check('写入后目录已创建', os.path.isdir(session_dir), session_dir)
    check('路径包含组别', 'A组' in db.db_path, db.db_path)


def test_list_sessions_skips_empty(tmp: str) -> None:
    print('\n[4] 会话列表：跳过空库、递归子目录、计入 -wal')
    data = os.path.join(tmp, 'data')
    os.makedirs(os.path.join(data, '1', '李四_20261030_160000'), exist_ok=True)

    # a) 真库：两条数据
    good = SensorDB(data_dir=data, db_name='good.db')
    good.insert('ecg_data', [1.0])
    good.insert('ecg_data', [2.0])
    good.close()

    # b) 空库：手工造出「只有文件头」的状态，等价于旧版构造即建库
    empty_path = os.path.join(data, '1', '李四_20261030_160000', 'empty.db')
    conn = sqlite3.connect(empty_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.close()
    check('空库尺寸等于一页', os.path.getsize(empty_path) == EMPTY_DB_BYTES,
          '实际 {}'.format(os.path.getsize(empty_path)))
    check('is_empty_db 判定为空', is_empty_db(empty_path))

    # c) WAL 里有数据、主库还没 checkpoint —— 总大小必须算上 -wal
    wal_db = os.path.join(data, 'wal_pending.db')
    conn = sqlite3.connect(wal_db)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute('CREATE TABLE ecg_data (time TEXT, value TEXT)')
    conn.executemany('INSERT INTO ecg_data VALUES (?,?)',
                     [('t{}'.format(i), str(i)) for i in range(200)])
    conn.commit()          # 故意不 close：数据仍在 -wal 里
    main_size = os.path.getsize(wal_db)
    check('主库本身仍然很小', main_size <= EMPTY_DB_BYTES, '实际 {}'.format(main_size))
    check('总大小已计入 WAL', db_total_size(wal_db) > EMPTY_DB_BYTES)
    check('不会误判为空库', not is_empty_db(wal_db))

    listed = {os.path.basename(p): s for p, _m, s in list_sessions(data)}
    conn.close()

    check('空库不出现在列表里', 'empty.db' not in listed, str(sorted(listed)))
    check('子目录里的会话能列出（递归生效）', 'good.db' in listed or 'wal_pending.db' in listed)
    check('正常库大小可读', listed.get('good.db', 0) >= EMPTY_DB_BYTES)
    check('WAL 中的库大小不为 0', listed.get('wal_pending.db', 0) > EMPTY_DB_BYTES)

    # d) 列表里不应出现 -wal / -shm 这类附属文件
    check('附属文件未被当成会话', not any(n.endswith(('-wal', '-shm')) for n in listed))


def test_format_size() -> None:
    print('\n[5] 大小显示：不足 1 MB 用 KB，不再一律 0.0 MB')
    cases = [(0, '0 B'), (900, '900 B'), (4200, '4 KB'), (280 * 1024, '280 KB'),
             (int(1.5 * 1024 * 1024), '1.5 MB'), (int(2.25 * 1024 ** 3), '2.25 GB')]
    for size, want in cases:
        got = format_size(size)
        check('format_size({}) == {}'.format(size, want), got == want, '实际 {}'.format(got))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix='hcp_empty_db_')
    try:
        test_no_file_before_first_write(tmp)
        test_file_created_on_first_write(tmp)
        test_session_subdir_lazy(tmp)
        test_list_sessions_skips_empty(tmp)
        test_format_size()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(PASSED) + len(FAILED)
    print('\n{} / {} 通过'.format(len(PASSED), total))
    if FAILED:
        print('失败项：' + '、'.join(FAILED))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
