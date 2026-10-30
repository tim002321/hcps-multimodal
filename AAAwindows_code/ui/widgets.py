# -*- coding: utf-8 -*-
"""通用控件：蓝色分区条、白面板、数值块、黑色曲线画布、待选/已选参数面板。"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt5.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ui import theme

pg.setConfigOptions(antialias=True)


def label(text: str, role: Optional[str] = None) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    return widget


def mono(text: str = '') -> QLabel:
    return label(text, 'Mono')


def pill(text: str, color: str = theme.PALETTE['blue'], bg: str = '#EAF2FB',
         border: str = '#9DBBDD') -> QLabel:
    widget = QLabel(text)
    widget.setStyleSheet(
        f'border-radius: 2px; padding: 3px 10px; font-size: 11px;'
        f'background-color: {bg}; border: 1px solid {border}; color: {color};')
    return widget


def dot(color: str, size: int = 8) -> QLabel:
    widget = QLabel()
    widget.setFixedSize(size, size)
    widget.setStyleSheet(f'background-color: {color}; border: 1px solid #808080;')
    return widget


class SectionBar(QFrame):
    """蓝色分区标题条（白字加粗），对应参考界面里的「训练科目设置 / 系统运行控制」那种。"""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('SectionBar')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)
        self.title = label(title, '')
        self.title.setStyleSheet('color: #FFFFFF; font-weight: bold; font-size: 12px;'
                                 ' background: transparent; border: none;')
        layout.addWidget(self.title)
        layout.addStretch(1)
        self.setMinimumHeight(24)

    def set_title(self, text: str) -> None:
        self.title.setText(text)


class Card(QFrame):
    """白底面板；有标题时顶部加一条蓝色分区条。"""

    def __init__(self, title: str = '', hint: str = '', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Card')
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        if title:
            self.section = SectionBar(title)
            root.addWidget(self.section)
        else:
            self.section = None
        body_holder = QWidget()
        self.body = QVBoxLayout(body_holder)
        self.body.setContentsMargins(8, 6, 8, 8)
        self.body.setSpacing(6)
        root.addWidget(body_holder, 1)
        if hint:
            self.body.addWidget(label(hint, 'CardHint'))

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout, stretch: int = 0):
        self.body.addLayout(layout, stretch)
        return layout


class StatTile(QFrame):
    """紧凑数值块：小标签 + 大数字（参考界面里参数名后面的数值）。"""

    def __init__(self, title: str, unit: str = '', value: str = '--',
                 accent: str = theme.PALETTE['red'], note: str = '',
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Card')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        layout.addWidget(label(title, 'StatLabel'))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.value = label(value, 'StatValue')
        self.value.setStyleSheet(f'font-family: "Consolas"; font-size: 20px; font-weight: bold;'
                                 f' color: {accent};')
        row.addWidget(self.value)
        if unit:
            row.addWidget(label(unit, 'StatUnit'))
        row.addStretch(1)
        layout.addLayout(row)
        self.note = label(note, 'StatLabelFaint')
        self.note.setVisible(bool(note))
        layout.addWidget(self.note)

    def set_value(self, value: str, note: Optional[str] = None, accent: Optional[str] = None) -> None:
        self.value.setText(value)
        if accent:
            self.value.setStyleSheet(f'font-family: "Consolas"; font-size: 20px;'
                                     f' font-weight: bold; color: {accent};')
        if note is not None:
            self.note.setText(note)
            self.note.setVisible(bool(note))


class NavItem(QPushButton):
    """左侧蓝色导航块。"""

    def __init__(self, badge: str, text: str, parent: Optional[QWidget] = None):
        super().__init__(f'{badge}  {text}', parent)
        self.setObjectName('NavItem')
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(32)


class PageBackdrop(QWidget):
    """浅灰底 + 极淡网格（参考界面是白底，这里保留一点点网格避免大片死白）。"""

    def paintEvent(self, event):                      # noqa: N802
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor('#ECECEC'))
        painter.setPen(QPen(QColor(46, 109, 180, 12), 1))
        step = 48
        for x in range(0, rect.width(), step):
            painter.drawLine(x, 0, x, rect.height())
        for y in range(0, rect.height(), step):
            painter.drawLine(0, y, rect.width(), y)
        painter.end()


TechBackdrop = PageBackdrop                            # 兼容旧名字


class ChartPanel(QFrame):
    """黑色曲线画布：顶部「参数名 —— 当前值」，画布里一条黄色竖游标。"""

    COLORS = ['#00D0FF', '#00E676', '#FFD700', '#FF5252', '#FF80AB', '#B388FF']

    def __init__(self, title: str = '', unit: str = '', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Card')
        self._curves: Dict[str, pg.PlotDataItem] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        self.title_label = label(title, 'CardTitle')
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.value_label = QLabel('--')
        self.value_label.setStyleSheet('font-family: "Consolas"; font-size: 14px;'
                                       ' color: #C00000; background: transparent;')
        head.addWidget(self.value_label)
        self.unit_label = label(unit, 'CardHint')
        head.addWidget(self.unit_label)
        layout.addLayout(head)

        self.plot = pg.PlotWidget(background=theme.PALETTE['plot_bg'])
        self.plot.setMinimumHeight(150)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        for axis in ('left', 'bottom'):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen('#808080'))
            ax.setTextPen(pg.mkPen(theme.PALETTE['plot_text']))
        self._cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen('#FFD700', width=1))
        self.plot.addItem(self._cursor)
        layout.addWidget(self.plot, 1)

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)

    def add_curve(self, name: str, color: Optional[str] = None, width: float = 1.4) -> None:
        if name in self._curves:
            return
        color = color or self.COLORS[len(self._curves) % len(self.COLORS)]
        self._curves[name] = self.plot.plot([], [], pen=pg.mkPen(color=color, width=width))

    def set_series(self, name: str, values: Sequence[float], sample_rate: float = 1.0,
                   color: Optional[str] = None, end_time: Optional[float] = None) -> None:
        """更新曲线。

        `end_time`（秒）是这段数据末点对应的**会话绝对时间**。传入后横轴从 0 开始
        一直正着加（`0, 0.004, …`），而不是以前那种 `-6 … 0` 的负计数。
        不传时退回旧行为（末点对齐到 0），仅为兼容尚未改造的调用点。
        """
        if name not in self._curves:
            self.add_curve(name, color)
        y = np.asarray(values, dtype=float)
        if y.size == 0:
            self._curves[name].setData([], [])
            return
        rate = sample_rate if sample_rate and sample_rate > 0 else 1.0
        if end_time is not None:
            x = end_time - (y.size - 1 - np.arange(y.size, dtype=float)) / rate
        else:
            x = np.arange(y.size, dtype=float) / rate
            x -= x[-1]
        self._curves[name].setData(x, y)
        # 黄色游标跟着数据末端走（以前固定在 0，是因为末点恒为 0）
        if self._cursor is not None:
            self._cursor.setPos(float(x[-1]))


    def getValue(self) -> None:                       # noqa: N802 - 与旧接口保持兼容
        return None


class ParamListPanel(QWidget):
    """「待选参数 / 已选参数」双列表 + 添加/删除（对应参考界面右侧那两栏）。"""

    changed = pyqtSignal(list)

    def __init__(self, available: List[tuple], selected: List[str],
                 title: str = '曲线显示参数', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.available = []                            # [(key, 显示名, 单位)]
        for item in available:
            if len(item) == 2:
                self.available.append((item[0], item[1], ''))
            else:
                self.available.append((item[0], item[1], item[2]))
        self.selected = [key for key in selected]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(SectionBar(title))

        layout.addWidget(label('待选参数：', 'Dim'))
        self.avail_list = self._make_table()
        layout.addWidget(self.avail_list, 1)

        buttons = QHBoxLayout()
        self.add_button = QPushButton('添加 ▶')
        self.add_button.clicked.connect(self._add)
        self.remove_button = QPushButton('◀ 删除')
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        layout.addLayout(buttons)

        layout.addWidget(label('已选参数：', 'Dim'))
        self.sel_list = self._make_table()
        layout.addWidget(self.sel_list, 1)

        self._refresh()

    @staticmethod
    def _make_table() -> QTableWidget:
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(['序号', '参数名', '单位'])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.setMinimumHeight(150)
        return table

    @staticmethod
    def _current_key(table: QTableWidget) -> Optional[str]:
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 1)
        return item.data(Qt.UserRole) if item is not None else None

    def _fill(self, table: QTableWidget, keys: List[str]) -> None:
        names = {key: (title, unit) for key, title, unit in self.available}
        table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            title, unit = names.get(key, (key, ''))
            name_item = QTableWidgetItem(title)
            name_item.setData(Qt.UserRole, key)
            table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            table.setItem(row, 1, name_item)
            table.setItem(row, 2, QTableWidgetItem(unit))

    def _refresh(self) -> None:
        available_keys = [key for key, _title, _unit in self.available if key not in self.selected]
        self._fill(self.avail_list, available_keys)
        self._fill(self.sel_list, list(self.selected))
        self.changed.emit(list(self.selected))

    def _add(self) -> None:
        key = self._current_key(self.avail_list)
        if key is None:
            return
        if key not in self.selected:
            self.selected.append(key)
        self._refresh()

    def _remove(self) -> None:
        key = self._current_key(self.sel_list)
        if key is None:
            return
        if key in self.selected:
            self.selected.remove(key)
        self._refresh()
