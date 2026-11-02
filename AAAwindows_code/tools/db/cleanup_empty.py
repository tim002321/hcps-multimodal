#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理「零张表的空库」——程序启动即建库、又没采到数据留下的 4096 字节文件。

用法（默认只报告，不动任何文件）：
    python tools/db/cleanup_empty.py                 # 扫描并列出待清理项
    python tools/db/cleanup_empty.py --delete --yes  # 真正删除

判定标准只有一个：主库 + -wal/-shm/-journal 合计 ≤ 一页（4096 字节），
即「SQLite 文件头 + 没有任何表」。有表但没数据的库不在清理范围内。
删完文件后会顺手移除变空的会话目录（如 data/1/李四_20261030_160811/）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from core.config import DATA_DIR                       # noqa: E402
from core.db_storage import DB_SUFFIXES, db_total_size, is_empty_db   # noqa: E402
from ui.replay import format_size                      # noqa: E402


def scan_empty(data_dir: str):
    """返回 [(路径, 占用字节)]，按路径排序。"""
    found = []
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            if not name.lower().endswith('.db'):
                continue
            path = os.path.join(root, name)
            if is_empty_db(path):
                found.append((path, db_total_size(path)))
    found.sort()
    return found


def scan_empty_dirs(data_dir: str):
    """data 下（不含 data 自身）的空目录，深的内存优先。"""
    empties = []
    for root, dirs, files in os.walk(data_dir, topdown=False):
        if os.path.abspath(root) == os.path.abspath(data_dir):
            continue
        try:
            if not os.listdir(root):
                empties.append(root)
        except OSError:
            continue
    return sorted(empties)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='清理零张表的空库（默认只报告）')
    parser.add_argument('--data-dir', default=DATA_DIR, help='数据目录，默认 core.config.DATA_DIR')
    parser.add_argument('--delete', action='store_true', help='真正删除（否则只报告）')
    parser.add_argument('--yes', action='store_true', help='删除时跳过交互确认')
    args = parser.parse_args(argv)

    data_dir = args.data_dir
    if not os.path.isdir(data_dir):
        print('数据目录不存在：{}'.format(data_dir))
        return 1

    empties = scan_empty(data_dir)
    total = sum(size for _p, size in empties)
    print('数据目录：{}'.format(data_dir))
    print('空库数量：{}，占用 {}'.format(len(empties), format_size(total)))
    for path, size in empties:
        rel = os.path.relpath(path, data_dir)
        print('  {:>6}  {}'.format(format_size(size), rel))

    if not empties:
        print('没有需要清理的空库。')
        return 0

    if not args.delete:
        print('\n这只是扫描结果，没有删除任何文件。')
        print('确认后执行：python tools/db/cleanup_empty.py --delete --yes')
        return 0

    if not args.yes:
        answer = input('\n将永久删除上面 {} 个空库（共 {}），输入 yes 继续：'
                       .format(len(empties), format_size(total)))
        if answer.strip().lower() != 'yes':
            print('已取消。')
            return 1

    removed = 0
    failed = 0
    for path, _size in empties:
        for suffix in DB_SUFFIXES:
            target = path + suffix
            if not os.path.exists(target):
                continue
            try:
                os.remove(target)
            except OSError as exc:
                print('  删除失败 {}：{}'.format(target, exc))
                failed += 1
        removed += 1

    dirs = scan_empty_dirs(data_dir)
    for folder in dirs:
        try:
            shutil.rmtree(folder)       # 只可能是空目录
        except OSError as exc:
            print('  目录删除失败 {}：{}'.format(folder, exc))

    print('\n已删除空库 {} 个，回收 {}；顺带清理空目录 {} 个。'
          .format(removed, format_size(total), len(dirs)))
    if failed:
        print('有 {} 个文件删除失败，请检查是否被其他程序占用。'.format(failed))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
