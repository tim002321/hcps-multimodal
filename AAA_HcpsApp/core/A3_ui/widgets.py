# -*- coding: utf-8 -*-
"""A3 界面的通用控件：科技感卡片、状态块、导航项、标题栏、图表面板、背景。

界面层只做"显示和转发"，不碰采集/存储/算法：曲线数据由 CollectorManager 的 sink 送来，
分析结果由 A2 算好后交给这里画。
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence

import pyqtgraph as pg
from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap)
from PyQt5.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QVBoxLayout, QWidget)

from core.A3_ui import theme

pg.setConfigOptions(antialias=True)


# ---------------------------------------------------------------- 小工具
def label(text: str, role: Optional[str] = None) -> QLabel:
    """建一个带样式角色的 QLabel（角色名 = QSS 里的 #Id）。"""
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    return widget


def mono(text: str = '') -> QLabel:
    return label(text, 'Mono')


def pill(text: str, color: str = theme.PALETTE['cyan'], bg: str = '#10293F',
         border: str = '#1C4A63') -> QLabel:
    """胶囊标签（顶栏状态、会话名等）。"""
    widget = QLabel(text)
    widget.setStyleSheet(
        f'border-radius: 11px; padding: 5px 14px; font-size: 12px;'
        f'background-color: {bg}; border: 1px solid {border}; color: {color};')
    return widget


def dot(color: str, size: int = 8) -> QLabel:
    """状态小圆点。"""
    widget = QLabel()
    widget.setFixedSize(size, size)
    widget.setStyleSheet(f'background-color: {color}; border-radius: {size // 2}px;')
    return widget


class Card(QFrame):
    """带标题的玻璃面板。add() 的控件进内容区。"""

    def __init__(self, title: str = '', hint: str = '', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Card')
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(10)

        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        if title:
            self.header.addWidget(label(title, 'CardTitle'))
        if hint:
            self.header.addWidget(label(hint, 'CardHint'))
        self.header.addStretch(1)
        root.addLayout(self.header)

        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        root.addLayout(self.body, 1)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout, stretch: int = 0):
        self.body.addLayout(layout, stretch)
        return layout

    def add_header_widget(self, widget: QWidget) -> QWidget:
        self.header.addWidget(widget)
        return widget


class StatTile(QFrame):
    """一块大数字：上方标签，中间数值 + 单位，下方补充说明。"""

    def __init__(self, title: str, unit: str = '', value: str = '--',
                 accent: str = theme.PALETTE['cyan'], note: str = '',
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Card')
        self.accent = accent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(dot(accent))
        top.addWidget(label(title, 'StatLabel'))
        top.addStretch(1)
        self.dot = top.itemAt(0).widget()
        layout.addLayout(top)

        value_row = QHBoxLayout()
        value_row.setSpacing(6)
        self.value = label(value, 'StatValue')
        self.value.setStyleSheet(f'font-family: "Consolas"; font-size: 26px; font-weight: 700;'
                                 f'color: {accent};')
        value_row.addWidget(self.value)
        if unit:
            unit_label = label(unit, 'StatUnit')
            unit_label.setAlignment(Qt.AlignBottom)
            value_row.addWidget(unit_label)
        value_row.addStretch(1)
        layout.addLayout(value_row)

        self.note = label(note, 'StatLabelFaint')
        layout.addWidget(self.note)

    def set_value(self, value: str, note: Optional[str] = None, accent: Optional[str] = None) -> None:
        self.value.setText(value)
        if accent and accent != self.accent:
            self.accent = accent
            self.value.setStyleSheet(f'font-family: "Consolas"; font-size: 26px; font-weight: 700;'
                                     f'color: {accent};')
            self.dot.setStyleSheet(f'background-color: {accent}; border-radius: 4px;')
        if note is not None:
            self.note.setText(note)


class NavItem(QPushButton):
    """左侧导航项：序号 + 文字（用 ASCII 数字，避免 Unicode 图标在字库里缺字形变方块）。"""

    def __init__(self, badge: str, text: str, parent: Optional[QWidget] = None):
        super().__init__(f'  {badge}    {text}', parent)
        self.setObjectName('NavItem')
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(40)


class TechBackdrop(QWidget):
    """页面背景：深色渐变 + 科技网格；右下角可叠一张半透明项目照片。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Backdrop')
        self._hero: Optional[QPixmap] = None
        path = theme.background_path()
        if path and os.path.exists(path):
            self._hero = QPixmap(path)

    def paintEvent(self, event):                      # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        gradient = QLinearGradient(0, 0, rect.width(), rect.height())
        gradient.setColorAt(0.0, QColor(theme.PALETTE['bg']))
        gradient.setColorAt(0.55, QColor('#0A1120'))
        gradient.setColorAt(1.0, QColor('#0C1626'))
        painter.fillRect(rect, gradient)

        # 网格
        painter.setPen(QPen(QColor(34, 211, 238, 14), 1))
        step = 44
        for x in range(0, rect.width(), step):
            painter.drawLine(x, 0, x, rect.height())
        for y in range(0, rect.height(), step):
            painter.drawLine(0, y, rect.width(), y)

        # 右上角青色光晕
        glow = QLinearGradient(rect.width() * 0.55, 0, rect.width(), rect.height() * 0.6)
        glow.setColorAt(0.0, QColor(34, 211, 238, 0))
        glow.setColorAt(1.0, QColor(34, 211, 238, 26))
        painter.fillRect(rect, glow)

        # 右下角项目照片（淡淡的一层，别抢内容）
        if self._hero is not None and not self._hero.isNull():
            scaled = self._hero.scaledToWidth(int(rect.width() * 0.38), Qt.SmoothTransformation)
            x = rect.width() - scaled.width()
            y = rect.height() - scaled.height()
            painter.setOpacity(0.10)
            painter.drawPixmap(x, y, scaled)
            painter.setOpacity(1.0)
            fade = QLinearGradient(0, y, 0, rect.height())
            fade.setColorAt(0.0, QColor(12, 22, 38, 90))
            fade.setColorAt(1.0, QColor(12, 22, 38, 0))
            painter.fillRect(QRectF(x, y, scaled.width(), scaled.height()), fade)
        painter.end()


class ChartPanel(QFrame):
    """pyqtgraph 折线面板：自动配色、图例、按名字追加数据。"""

    COLORS = ['#22D3EE', '#F59E0B', '#8B5CF6', '#34D399', '#EF4444', '#3B82F6']

    def __init__(self, title: str = '', y_label: str = '', max_points: int = 600,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('Card')
        self.max_points = max_points
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._xs: Dict[str, List[float]] = {}
        self._ys: Dict[str, List[float]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(6)
        if title:
            layout.addWidget(label(title, 'CardTitle'))

        self.plot = pg.PlotWidget(background=theme.PALETTE['bg_soft'])
        self.plot.setMinimumHeight(150)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        for axis in ('left', 'bottom'):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen(theme.PALETTE['line']))
            ax.setTextPen(pg.mkPen(theme.PALETTE['text_dim']))
        if y_label:
            self.plot.setLabel('left', y_label, color=theme.PALETTE['text_dim'])
        self.legend = self.plot.addLegend(offset=(-10, 8), labelTextColor=theme.PALETTE['text_dim'])
        layout.addWidget(self.plot, 1)

    def add_curve(self, name: str, color: Optional[str] = None, width: float = 1.8) -> None:
        if name in self._curves:
            return
        color = color or self.COLORS[len(self._curves) % len(self.COLORS)]
        pen = pg.mkPen(color=color, width=width)
        self._curves[name] = self.plot.plot([], [], pen=pen, name=name)
        self._xs[name] = []
        self._ys[name] = []

    def append(self, name: str, value: float, x: Optional[float] = None) -> None:
        if name not in self._curves:
            self.add_curve(name)
        xs, ys = self._xs[name], self._ys[name]
        xs.append(float(x) if x is not None else float(len(xs)))
        ys.append(float(value))
        if len(xs) > self.max_points:
            del xs[:len(xs) - self.max_points]
            del ys[:len(ys) - self.max_points]
        self._curves[name].setData(xs, ys)

    def set_series(self, name: str, xs: Sequence[float], ys: Sequence[float],
                   color: Optional[str] = None) -> None:
        if name not in self._curves:
            self.add_curve(name, color)
        self._xs[name] = [float(v) for v in xs]
        self._ys[name] = [float(v) for v in ys]
        self._curves[name].setData(self._xs[name], self._ys[name])

    def clear(self) -> None:
        for name, curve in self._curves.items():
            self._xs[name] = []
            self._ys[name] = []
            curve.setData([], [])


class PacketBridge(QObject):
    """CollectorManager 的 sink：把采集线程的包搬到 Qt 主线程。

    采集线程 emit -> Qt 自动排队到 UI 线程，界面直接连这个信号就行，
    不用再像老工程那样让采集线程往 127.0.0.1:8008 发 UDP 回环。
    """

    packet = pyqtSignal(object)

    def __call__(self, packet) -> None:               # add_sink 直接接收 callable
        self.packet.emit(packet)


class LogBridge(QObject):
    """logging -> Qt 信号。

    **必须这么绕一圈**：采集器是在自己的线程里打日志的（A0 每个采集器一个线程），
    而 Qt 控件只能在 GUI 线程里改。之前把 logging 直接接到 QTextEdit 上，
    非 GUI 线程去动文本控件，Qt 内部对象处于半销毁状态，就出现
    "0x0000000000000008 内存不能为 read" 这种访问违例（本工程实测：一截图就崩）。
    这里只发信号，Qt 会按接收者所在线程自动排队，改动落到 GUI 线程。
    """

    message = pyqtSignal(str, int)

    def emit_record(self, text: str, level: int) -> None:
        self.message.emit(text, level)


class BarMeter(QWidget):
    """自绘压力条（比 QProgressBar 更好看，带刻度与数值）。"""

    def __init__(self, title: str, maximum: float = 5000.0, color: str = theme.PALETTE['cyan'],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.title = title
        self.maximum = float(maximum)
        self.value = 0.0
        self.color = QColor(color)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, value: float) -> None:
        self.value = max(0.0, float(value))
        self.update()

    def paintEvent(self, event):                      # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(Qt.NoPen)

        bar_rect = QRectF(0, rect.height() - 14, rect.width() * 0.78, 10)
        painter.setBrush(QColor(theme.PALETTE['bg_soft']))
        painter.drawRoundedRect(bar_rect, 5, 5)

        ratio = min(1.0, self.value / self.maximum) if self.maximum else 0.0
        if ratio > 0:
            fill = QRectF(bar_rect)
            fill.setWidth(max(6.0, bar_rect.width() * ratio))
            gradient = QLinearGradient(fill.topLeft(), fill.topRight())
            gradient.setColorAt(0.0, self.color.darker(140))
            gradient.setColorAt(1.0, self.color)
            painter.setBrush(gradient)
            painter.drawRoundedRect(fill, 5, 5)

        painter.setPen(QPen(QColor(theme.PALETTE['text_dim'])))
        font = QFont(theme.FONT_UI, 11)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, rect.width() * 0.78, 14), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        font.setFamily(theme.FONT_MONO)
        painter.setFont(font)
        painter.drawText(QRectF(bar_rect.right() + 8, rect.height() - 16, rect.width() * 0.2, 14),
                         Qt.AlignRight | Qt.AlignVCenter, f'{self.value:,.0f}')
        painter.end()
