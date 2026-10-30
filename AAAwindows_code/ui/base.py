# -*- coding: utf-8 -*-
"""页面基类与曲线网格页：所有页面共用的骨架。

从 `window.py` 抽出来，是为了让新增页面（采集/数据查看/状态识别/数据分析）
能独立成文件，不必都堆进主窗口那一个 1200 行的文件里。

`window.py` 只负责窗口骨架与导航，不负责具体页面内容。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt5.QtWidgets import (QGridLayout, QHBoxLayout, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from ui import theme
from ui.widgets import ChartPanel, ParamListPanel, mono, pill

# 曲线配色：键与各页面 CHANNELS 的 key 对应
CHART_COLORS = {
    'ecg': '#00D0FF', 'resp': '#00E676', 'accx': '#FFD700',
    'accy': '#FF5252', 'accz': '#B388FF', 'temp': '#FF80AB',
    'ppg1': '#00D0FF', 'ppg2': '#00E676', 'ppg3': '#FFD700',
    'ppg4': '#FF5252', 'gsr': '#00E676', 'wx': '#FFD700',
    'wy': '#FF5252', 'wz': '#B388FF',
    'emg1': '#00D0FF', 'emg2': '#00E676',
    'eye_x': '#FFD700', 'eye_y': '#FF5252', 'pupil': '#B388FF',
}


def ac_couple(values: Any, window: int = 120) -> np.ndarray:
    """去掉直流基线（PPG 是 24bit 原始值，直流分量几百万，不去掉看不出脉搏波）。"""
    y = np.asarray(values, dtype=float)
    if y.size <= 2:
        return y
    window = max(3, min(int(window), y.size))
    if window % 2 == 0:
        window += 1
    pad = window // 2
    padded = np.pad(y, (pad, pad), mode='edge')      # 两端按边缘延拓，避免卷积补 0 造成边沿尖峰
    baseline = np.convolve(padded, np.ones(window) / window, mode='same')[pad:pad + y.size]
    return y - baseline


class BasePage(QWidget):
    """所有页面的基类：自带滚动容器与三个生命周期钩子。"""

    title = '页面'
    subtitle = ''

    def __init__(self, window: 'object'):
        super().__init__()
        self.window_ref = window
        from ui.widgets import PageBackdrop

        self.backdrop = PageBackdrop(self)
        self.backdrop.setGeometry(self.rect())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.content = QVBoxLayout(inner)
        self.content.setContentsMargins(10, 8, 10, 10)
        self.content.setSpacing(8)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.build()

    def resizeEvent(self, event):                     # noqa: N802
        self.backdrop.setGeometry(self.rect())
        super().resizeEvent(event)

    def build(self) -> None:
        """子类搭页面。"""

    def on_data(self, snapshot: Dict[str, Any]) -> None:
        """约 25 帧/秒（40ms）。"""

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        """每秒。"""


class ChannelGridPage(BasePage):
    """通用「曲线网格 + 待选/已选参数」页面。"""

    CHANNELS: List[Dict[str, str]] = []
    DEFAULT_SELECTED: List[str] = []
    COLUMNS = 2

    def build(self) -> None:
        self.tiles_row = QHBoxLayout()
        self.tiles_row.setSpacing(8)
        self.status_pill = pill('等待数据', theme.PALETTE['text_dim'], '#F0F0F0', '#B0B0B0')
        self.content.addLayout(self.tiles_row)
        self.build_tiles()
        self.tiles_row.addWidget(self.status_pill, 0)

        body = QHBoxLayout()
        body.setSpacing(8)
        left = QVBoxLayout()
        left.setSpacing(6)
        self.grid_holder = QWidget()
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_holder)
        left.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        self.clear_button = QPushButton('清空曲线')
        self.clear_button.clicked.connect(self._clear_curves)
        bottom.addWidget(self.clear_button)
        self.info_label = mono('—')
        bottom.addWidget(self.info_label)
        bottom.addStretch(1)
        left.addLayout(bottom)
        body.addLayout(left, 1)

        self.param_panel = ParamListPanel(
            [(channel['key'], channel['title'], channel.get('unit', ''))
             for channel in self.CHANNELS],
            self.DEFAULT_SELECTED, '曲线显示参数')
        self.param_panel.setFixedWidth(236)
        self.param_panel.changed.connect(self._rebuild_charts)
        body.addWidget(self.param_panel, 0)
        self.content.addLayout(body, 1)

        self.charts: Dict[str, ChartPanel] = {}
        self._rebuild_charts(list(self.DEFAULT_SELECTED))

    # 子类实现
    def build_tiles(self) -> None:
        """在 self.tiles_row 里放数值块。"""

    def channel_data(self, snapshot: Dict[str, Any]) -> Dict[str, Tuple[Any, float, str]]:
        """返回 {key: (数据序列, 采样率, 当前值文本)}。"""
        return {}

    def channel_end_time(self, snapshot: Dict[str, Any], key: str) -> Optional[float]:
        """该通道末点对应的会话绝对时间（秒）。

        返回 None 则横轴退回旧的「末点对齐 0」行为。子类用计数器换算，
        例如心电 250Hz 下第 N 个采样点就是 N/250 秒，保证横轴只增不减。
        """
        return None

    # 通用实现
    def _rebuild_charts(self, keys: List[str]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.charts = {}
        by_key = {channel['key']: channel for channel in self.CHANNELS}
        for index, key in enumerate(keys):
            channel = by_key.get(key)
            if channel is None:
                continue
            chart = ChartPanel(channel['title'], channel.get('unit', ''))
            self.charts[key] = chart
            self.grid.addWidget(chart, index // self.COLUMNS, index % self.COLUMNS)
        for column in range(self.COLUMNS):
            self.grid.setColumnStretch(column, 1)

    def _clear_curves(self) -> None:
        self.window_ref.buffers.reset()
        for chart in self.charts.values():
            for name in list(chart._curves):
                chart.set_series(name, [], 1.0)

    def on_data(self, snapshot: Dict[str, Any]) -> None:
        data = self.channel_data(snapshot)
        for key, chart in self.charts.items():
            if key not in data:
                continue
            values, rate, text = data[key]
            chart.set_series(key, values, rate, CHART_COLORS.get(key),
                             end_time=self.channel_end_time(snapshot, key))
            chart.set_value(text)
