# -*- coding: utf-8 -*-
"""数据库浏览工具：直接翻会话库（SQLite）里的表和记录。

用法：
    ..\\shifeiyuantwo\\Scripts\\python.exe 数据库浏览.py
    ..\\shifeiyuantwo\\Scripts\\python.exe 数据库浏览.py --db data\\心算\\...\\xxx.db

功能：
  * 列出 data\\ 下所有会话库（含打包版旁边的 data\\），按修改时间倒序
  * 左侧选表，显示行数；右侧分页看数据（每页 200 行，大库也不会卡）
  * 支持写一句 SELECT 自己查（只读连接，写不进去）
  * 一键把当前表导成 CSV（流式写，几百万行也不吃内存）

只读打开：所有连接都是 mode=ro，浏览不会改动任何数据。
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QPushButton, QSplitter,
                             QTableWidget, QTableWidgetItem, QTextEdit,
                             QVBoxLayout, QWidget)

ROOT = os.path.dirname(os.path.abspath(__file__))
# 信号库排在最前：拆出来的小库（几十~几百 MB），日常查看优先用它，
# 免得为了看一段心电去开 8 GB 的原始库。
DEFAULT_DIRS = [os.path.join(ROOT, '信号库'),
                os.path.join(ROOT, 'AAAwindows_code', 'data'),
                os.path.join(ROOT, 'dist', 'HCPS', 'data')]
PAGE_SIZE = 200


def human_size(num: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num < 1024 or unit == 'GB':
            return '%.1f %s' % (num, unit)
        num /= 1024.0
    return '%.1f GB' % num


def find_databases() -> list:
    """扫 data 目录（含子目录）下所有 .db，按修改时间倒序。"""
    found = []
    for base in DEFAULT_DIRS:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.lower().endswith('.db'):
                    path = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    found.append((path, size, os.path.getmtime(path)))
    found.sort(key=lambda item: item[2], reverse=True)
    return found


class DbBrowser(QMainWindow):
    def __init__(self, db_path: str = ''):
        super().__init__()
        self.setWindowTitle('会话库浏览 · 只读')
        self.resize(1280, 820)
        self.con = None
        self.db_path = ''
        self.table = ''
        self.page = 0
        self.total_rows = None
        self._build()
        self.reload_databases(select=db_path)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(QLabel('数据库'))
        self.db_combo = QComboBox()
        self.db_combo.setMinimumWidth(520)
        self.db_combo.currentIndexChanged.connect(self.on_db_changed)
        top.addWidget(self.db_combo, 1)
        btn_open = QPushButton('打开文件…')
        btn_open.clicked.connect(self.open_file)
        top.addWidget(btn_open)
        btn_refresh = QPushButton('刷新')
        btn_refresh.clicked.connect(lambda: self.reload_databases())
        top.addWidget(btn_refresh)
        outer.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.addWidget(QLabel('表'))
        self.table_list = QListWidget()
        self.table_list.currentItemChanged.connect(self.on_table_changed)
        left_box.addWidget(self.table_list, 1)
        left.setMaximumWidth(320)
        split.addWidget(left)

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.info = QLabel('未打开数据库')
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(self.info, 1)
        self.btn_prev = QPushButton('上一页')
        self.btn_prev.clicked.connect(lambda: self.flip(-1))
        self.btn_next = QPushButton('下一页')
        self.btn_next.clicked.connect(lambda: self.flip(1))
        self.btn_count = QPushButton('统计行数')
        self.btn_count.clicked.connect(self.count_total)
        self.btn_export = QPushButton('导出 CSV')
        self.btn_export.clicked.connect(self.export_current)
        for btn in (self.btn_prev, self.btn_next, self.btn_count, self.btn_export):
            btn.setEnabled(False)
            bar.addWidget(btn)
        right_box.addLayout(bar)

        self.grid = QTableWidget(0, 0)
        self.grid.setAlternatingRowColors(True)
        mono = QFont('Consolas')
        mono.setPointSize(9)
        self.grid.setFont(mono)
        self.grid.setEditTriggers(QTableWidget.NoEditTriggers)
        right_box.addWidget(self.grid, 1)

        query_box = QVBoxLayout()
        query_box.addWidget(QLabel('自定义查询（只读，最多显示 1000 行）'))
        row = QHBoxLayout()
        self.sql_edit = QTextEdit()
        self.sql_edit.setFixedHeight(58)
        self.sql_edit.setPlaceholderText(
            'SELECT * FROM bracelet_ppg_data ORDER BY rowid DESC LIMIT 100')
        row.addWidget(self.sql_edit, 1)
        col = QVBoxLayout()
        btn_run = QPushButton('执行')
        btn_run.clicked.connect(self.run_sql)
        col.addWidget(btn_run)
        col.addStretch(1)
        row.addLayout(col)
        query_box.addLayout(row)
        right_box.addLayout(query_box)

        split.addWidget(right)
        split.setStretchFactor(1, 1)
        outer.addWidget(split, 1)

    # -------------------------------------------------------------- 数据库
    def reload_databases(self, select: str = '') -> None:
        self.db_combo.blockSignals(True)
        self.db_combo.clear()
        for path, size, _mtime in find_databases():
            rel = os.path.relpath(path, ROOT)
            self.db_combo.addItem('{}   ({})'.format(rel, human_size(size)), path)
        self.db_combo.blockSignals(False)
        if self.db_combo.count() == 0:
            self.info.setText('没有找到 .db，点「打开文件…」选一个')
            return
        index = 0
        if select:
            for i in range(self.db_combo.count()):
                if os.path.normcase(self.db_combo.itemData(i)) == os.path.normcase(select):
                    index = i
                    break
        self.db_combo.setCurrentIndex(index)
        self.open_database(self.db_combo.itemData(index))

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, '选择数据库', ROOT,
                                              'SQLite 数据库 (*.db *.sqlite *.sqlite3);;所有文件 (*)')
        if path:
            self.open_database(path)

    def open_database(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            if self.con is not None:
                self.con.close()
            # 只读打开：浏览不会改动数据
            self.con = sqlite3.connect(
                'file:{}?mode=ro'.format(path.replace('\\', '/')), uri=True)
            self.con.text_factory = bytes          # 大库里有非法 UTF-8，先拿字节自己解
        except sqlite3.Error as exc:
            QMessageBox.warning(self, '打不开', '{}'.format(exc))
            return

        self.db_path = path
        self.page = 0
        self.total_rows = None
        self.grid.clear()
        self.grid.setRowCount(0)
        self.grid.setColumnCount(0)
        self.table_list.clear()
        self.info.setText('已打开 {}'.format(os.path.basename(path)))

        for (name,) in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            label = name.decode('utf-8', 'replace') if isinstance(name, bytes) else str(name)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, label)
            self.table_list.addItem(item)

        if self.table_list.count():
            self.table_list.setCurrentRow(0)
        else:
            self.info.setText('这个库里没有表')

    def on_db_changed(self) -> None:
        self.open_database(self.db_combo.currentData())

    # ------------------------------------------------------------------ 表
    def current_table(self) -> str:
        item = self.table_list.currentItem()
        return item.data(Qt.UserRole) if item else ''

    def on_table_changed(self, *_args) -> None:
        name = self.current_table()
        if not name or self.con is None:
            return
        self.table = name
        self.page = 0
        self.total_rows = None          # 默认不统计：GB 级库上 COUNT(*) 会全表扫
        self.load_page()

    def count_rows(self, table: str):
        try:
            row = self.con.execute('SELECT COUNT(*) FROM "{}"'.format(table)).fetchone()
            return int(row[0])
        except sqlite3.Error:
            return None

    def load_page(self) -> None:
        if self.con is None or not self.table:
            return
        offset = self.page * PAGE_SIZE
        # 多取一行：只为判断「还有没有下一页」，避免每次翻页都 COUNT(*)
        sql = 'SELECT * FROM "{}" LIMIT {} OFFSET {}'.format(
            self.table, PAGE_SIZE + 1, offset)
        try:
            cursor = self.con.execute(sql)
        except sqlite3.Error as exc:
            self.info.setText('查询失败：{}'.format(exc))
            return
        has_more = self.fill_grid(cursor)
        total = self.total_rows
        if total is not None:
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            where = '第 {}/{} 页'.format(self.page + 1, pages)
        else:
            where = '第 {} 页（总行数未统计）'.format(self.page + 1)
        self.info.setText('{} · {} · 每页 {} 行'.format(self.table, where, PAGE_SIZE))
        self.btn_prev.setEnabled(self.page > 0)
        self.btn_next.setEnabled(
            has_more if total is None else (offset + PAGE_SIZE) < total)
        self.btn_count.setEnabled(total is None)
        self.btn_export.setEnabled(True)

    def count_total(self) -> None:
        """按需统计整表行数：GB 级库上可能要等十几秒。"""
        if self.con is None or not self.table:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.total_rows = self.count_rows(self.table)
        finally:
            QApplication.restoreOverrideCursor()
        self.load_page()
        self.btn_export.setEnabled(True)

    def flip(self, delta: int) -> None:
        page = self.page + delta
        if page < 0:
            return
        if self.total_rows is not None and page * PAGE_SIZE >= self.total_rows:
            return
        self.page = page
        self.load_page()

    # ---------------------------------------------------------------- 展示
    @staticmethod
    def _cell_text(value) -> str:
        if value is None:
            return 'NULL'
        if isinstance(value, float):
            # accuracy 这类 0.9615384615384616 太扎眼，留 4 位小数就够看
            text = ('%.4f' % value).rstrip('0').rstrip('.')
            return text or '0'
        if isinstance(value, bytes):
            try:
                text = value.decode('utf-8')
            except UnicodeDecodeError:
                return '<{} 字节二进制>'.format(len(value))
        else:
            text = str(value)
        text = text.replace('\n', ' ')
        return text if len(text) <= 300 else text[:300] + ' …'

    def fill_grid(self, cursor) -> bool:
        columns = [d[0] for d in (cursor.description or [])]
        rows = cursor.fetchall()
        has_more = len(rows) > PAGE_SIZE
        if has_more:
            rows = rows[:PAGE_SIZE]
        self.grid.setColumnCount(len(columns))
        self.grid.setRowCount(len(rows))
        self.grid.setHorizontalHeaderLabels(columns)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                self.grid.setItem(r, c, QTableWidgetItem(self._cell_text(value)))
        self.grid.resizeColumnsToContents()
        for c in range(self.grid.columnCount()):      # 单列别撑太宽
            if self.grid.columnWidth(c) > 320:
                self.grid.setColumnWidth(c, 320)
        return has_more

    # ---------------------------------------------------------------- 查询
    def run_sql(self) -> None:
        if self.con is None:
            return
        sql = self.sql_edit.toPlainText().strip().rstrip(';')
        if not sql:
            return
        if not sql.lower().startswith(('select', 'with', 'pragma')):
            QMessageBox.information(self, '只读', '这里只放行 SELECT / WITH / PRAGMA。')
            return
        try:
            cursor = self.con.execute(sql)
            self.fill_grid(cursor)
            self.info.setText('自定义查询返回 {} 行'.format(self.grid.rowCount()))
            self.btn_prev.setEnabled(False)
            self.btn_next.setEnabled(False)
            self.btn_export.setEnabled(True)
        except sqlite3.Error as exc:
            QMessageBox.warning(self, '查询失败', '{}'.format(exc))

    # ---------------------------------------------------------------- 导出
    def export_current(self) -> None:
        if self.con is None or not self.table:
            return
        default = os.path.join(os.path.dirname(self.db_path),
                               '{}.csv'.format(self.table))
        path, _ = QFileDialog.getSaveFileName(self, '导出当前表', default, 'CSV (*.csv)')
        if not path:
            return
        try:
            cursor = self.con.execute('SELECT * FROM "{}"'.format(self.table))
            columns = [d[0] for d in cursor.description]
            written = 0
            with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in cursor:                    # 流式写，不吃内存
                    writer.writerow([v.decode('utf-8', 'replace') if isinstance(v, bytes) else v
                                     for v in row])
                    written += 1
        except (sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, '导出失败', '{}'.format(exc))
            return
        self.info.setText('已导出 {} 行 → {}'.format(written, os.path.basename(path)))

    def closeEvent(self, event) -> None:              # noqa: N802 - Qt 命名
        if self.con is not None:
            self.con.close()
        event.accept()


def main() -> int:
    ap = argparse.ArgumentParser(description='会话库（SQLite）浏览工具，只读')
    ap.add_argument('--db', default='', help='直接打开某个 .db')
    args, qt_args = ap.parse_known_args()

    app = QApplication(sys.argv[:1] + qt_args)
    window = DbBrowser(args.db)
    window.show()
    return app.exec_()


if __name__ == '__main__':
    raise SystemExit(main())
