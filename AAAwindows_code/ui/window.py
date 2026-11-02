# -*- coding: utf-8 -*-
"""主窗口：心电贴 / 绿光手环 / 状态 三个页面（浅色工业风）。

版式参照现场数据监控界面：
    左侧蓝色导航 | 中间 2 列曲线网格（黑底、黄色游标）| 右侧「待选/已选参数」+ 添加/删除
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QImage, QPixmap
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
                             QGridLayout, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                             QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                             QProgressBar, QScrollArea, QStackedWidget, QTabWidget,
                             QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from core.config import DATA_DIR, Config
from core.device_status import STATE_TEXT, DeviceStatus
from devices.camera_receiver import CameraService
from ui import theme
from ui.base import BasePage, CHART_COLORS, ChannelGridPage, ac_couple
from ui.buffers import (ECG_ACCEL_RATE, ECG_RATE, RESP_RATE, LiveBuffers, ecg_signal_quality,
                        estimate_heart_rate, estimate_resp_rate, resp_has_signal)
from ui.pages_extra import AnalysisPage, EmgPage, EyePage, RecognizePage
from ui.profile import DATA_DEVICES
from ui.replay import (ReplayPlayer, format_size, instrument_spans, list_db_sources,
                       list_sessions, load_packets, overlap_window)
from ui.widgets import (Card, ChartPanel, NavItem, PageBackdrop, ParamListPanel,
                        StatTile, dot, label, mono, pill)
# 只导入线程与状态对象；torch/onnxruntime 在 YawnMonitor.run() 里才延迟加载，
# 所以没装推理依赖时界面照样能开，勾选后给可读报错即可。
from vision.yawn_monitor import YawnMonitor, YawnState

# 页面注册表：顺序 == 侧栏顺序 == self.pages 的顺序（switch_page 用扁平索引）。
# 每项 (key, 侧栏标题, 所属分区, 页面类名, 依赖的设备)。
#   依赖设备为 None → 任何情况都显示；
#   依赖设备有值   → 只有启动时勾了这台设备才注册这一页。
# 类名用字符串而不是直接写类对象：这些页面类定义在文件后面，写死会 NameError。
PAGE_SPECS = [
    ('prep',      '设备准备',   '采集',     'PrepPage',      None),
    ('ecg',       '心电',       '采集',     'EcgPage',       'ecg'),
    ('bracelet',  '手环',       '采集',     'BraceletPage',  'bracelet'),
    ('camera',    '摄像头',     '采集',     'CameraPage',    'camera'),
    ('emg',       '表面肌电',   '采集',     'EmgPage',       'emg'),
    ('eye',       '眼动仪',     '采集',     'EyePage',       'eye'),
    ('status',    '会话与回放', '数据查看', 'StatusPage',    None),
    ('recognize', '实时结果',   '状态识别', 'RecognizePage', None),
    ('analysis',  '区间分析',   '数据分析', 'AnalysisPage',  None),
]

ALL_DEVICES = ('ecg', 'bracelet', 'camera', 'emg', 'eye')

# 三段式流程：页面归属哪一段。没列出的都算采集段（prep/ecg/bracelet/camera/emg/eye/recognize）。
STAGE_SPECS = [
    ('采集', '连接设备并录制数据'),
    ('回放与选段', '从录制结果里挑出要分析的片段'),
    ('数据分析', '勾选模型出结果'),
]
STAGE_OF_KEY = {'status': 1, 'analysis': 2}


def stage_of_key(key: str) -> int:
    return STAGE_OF_KEY.get(key, 0)


class PrepPage(BasePage):
    """设备准备：一键连接心电贴 / 手环 / 摄像头，显示连接状态。"""

    title = '设备准备'
    subtitle = '连接心电贴、绿光手环与 USB 摄像头，全部就绪后开始采集'
    DEVICES = [('ecg', '心电贴', 'BLE 低功耗蓝牙'),
               ('bracelet', '绿光手环', 'BLE 低功耗蓝牙'),
               ('camera', 'USB 摄像头', '本地 UVC 设备')]

    def build(self) -> None:
        # 页面名由顶部标题栏负责，页内不再重复一遍
        banner_row = QHBoxLayout()
        banner_row.setSpacing(8)
        self.banner = QLabel('尚未连接设备，点击「开始连接」扫描蓝牙并打开摄像头')
        self.banner.setStyleSheet('background-color: #F7F7F7; border: 1px solid #B0B0B0;'
                                  ' padding: 8px 12px; color: #555555; font-weight: bold;')
        banner_row.addWidget(self.banner, 1)
        self.overall_pill = pill('未连接', theme.PALETTE['text_dim'], '#F0F0F0', '#B0B0B0')
        banner_row.addWidget(self.overall_pill, 0)
        self.content.addLayout(banner_row)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.device_widgets: Dict[str, Dict[str, Any]] = {}
        for key, title, hint in self.DEVICES:
            card = Card(title, hint)
            row = QHBoxLayout()
            marker = dot(theme.PALETTE['text_faint'])
            row.addWidget(marker)
            state = label('未开始', 'PageDesc')
            state.setStyleSheet(f'font-size: {theme.pt(theme.FS_SUB)}; font-weight: bold; color: #555555;')
            row.addWidget(state, 1)
            card.add_layout(row)
            detail = mono('—')
            detail.setWordWrap(True)
            card.add(detail)
            cards.addWidget(card, 1)
            self.device_widgets[key] = {'dot': marker, 'state': state, 'detail': detail}
        self.content.addLayout(cards)

        buttons = QHBoxLayout()
        self.connect_button = QPushButton('开始连接')
        self.connect_button.setObjectName('Primary')
        self.connect_button.setMinimumHeight(32)
        self.connect_button.clicked.connect(self._connect)
        buttons.addWidget(self.connect_button)
        self.rescan_button = QPushButton('重新扫描')
        self.rescan_button.clicked.connect(self._rescan)
        buttons.addWidget(self.rescan_button)
        self.disconnect_button = QPushButton('全部断开')
        self.disconnect_button.clicked.connect(self._disconnect)
        buttons.addWidget(self.disconnect_button)
        buttons.addStretch(1)
        self.content.addLayout(buttons)

        steps_card = Card('连接流程')
        self.step_labels: List[QLabel] = []
        for text in ('① 扫描蓝牙广播', '② 连接心电贴', '③ 连接绿光手环',
                     '④ 打开 USB 摄像头', '⑤ 设备就绪，可以开始采集'):
            line = QLabel('○  ' + text)
            line.setStyleSheet('padding: 3px 4px; color: #777777;')
            steps_card.add(line)
            self.step_labels.append(line)
        self.content.addWidget(steps_card)
        self.content.addStretch(1)

    # ------------------------------------------------------------- 操作
    def _connect(self) -> None:
        try:
            self.window_ref.connect_devices()
        except Exception as exc:                      # noqa: BLE001
            self.banner.setText('连接失败：{}'.format(exc))
            self.banner.setStyleSheet('background-color: #FDECEC; border: 1px solid #C00000;'
                                      ' padding: 8px 12px; color: #C00000; font-weight: bold;')

    def _rescan(self) -> None:
        self.window_ref.disconnect_devices()
        self.window_ref.connect_devices()

    def _disconnect(self) -> None:
        self.window_ref.disconnect_devices()

    # ------------------------------------------------------------- 刷新
    @staticmethod
    def _color(state: str) -> str:
        return {'connected': theme.PALETTE['green'],
                'connecting': theme.PALETTE['amber'],
                'scanning': theme.PALETTE['amber'],
                'error': theme.PALETTE['red'],
                'lost': theme.PALETTE['red'],
                'not_found': theme.PALETTE['red']}.get(state, '#B0B0B0')

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        states = self.window_ref.status.snapshot()
        for key, widgets in self.device_widgets.items():
            item = states.get(key, {})
            state = item.get('state', 'idle')
            color = self._color(state)
            widgets['dot'].setStyleSheet(
                'background-color: {}; border: 1px solid #808080;'.format(color))
            widgets['state'].setText(STATE_TEXT.get(state, state))
            widgets['state'].setStyleSheet(
                f'font-size: {theme.pt(theme.FS_SUB)}; font-weight: bold; color: {color};')
            detail = item.get('detail') or '—'
            widgets['detail'].setText(detail)

        ecg = states.get('ecg', {}).get('state', 'idle')
        bracelet = states.get('bracelet', {}).get('state', 'idle')
        camera = states.get('camera', {}).get('state', 'idle')
        ready = all(state == 'connected' for state in (ecg, bracelet, camera))
        if ready:
            self.overall_pill.setText('设备已就绪')
            self.overall_pill.setStyleSheet(
                f'border-radius: 2px; padding: 3px 12px; font-size: {theme.pt(theme.FS_BASE)};'
                f' background-color: #E8F5E9; border: 1px solid {theme.PALETTE["green"]};'
                f' color: {theme.PALETTE["green"]};')
            self.banner.setText('设备已全部连接，可以开始采集了')
            self.banner.setStyleSheet('background-color: #E8F5E9; border: 1px solid {};'
                                      ' padding: 8px 12px; color: {}; font-weight: bold;'
                                      .format(theme.PALETTE['green'], theme.PALETTE['green']))
            self.connect_button.setText('已连接')
        else:
            connected = [name for name, state in (('心电贴', ecg), ('手环', bracelet),
                                                  ('摄像头', camera)) if state == 'connected']
            self.overall_pill.setText('已连接 {}/3'.format(len(connected)))
            self.overall_pill.setStyleSheet(
                f'border-radius: 2px; padding: 3px 12px; font-size: {theme.pt(theme.FS_BASE)};'
                ' background-color: #F7F7F7; border: 1px solid #B0B0B0; color: #555555;')
            if not connected:
                self.banner.setText('尚未连接设备，点击「开始连接」扫描蓝牙并打开摄像头')
            else:
                self.banner.setText('已连接：{}　等待其余设备…'.format('、'.join(connected)))
            self.banner.setStyleSheet('background-color: #F7F7F7; border: 1px solid #B0B0B0;'
                                      ' padding: 8px 12px; color: #555555; font-weight: bold;')
            self.connect_button.setText('重新连接')

        step_states = [
            'done' if ecg in ('connecting', 'connected') or bracelet in ('connecting', 'connected')
            else ('run' if 'scanning' in (ecg, bracelet) else 'wait'),
            'done' if ecg == 'connected' else ('run' if ecg in ('scanning', 'connecting') else 'wait'),
            'done' if bracelet == 'connected' else
            ('run' if bracelet in ('scanning', 'connecting') else 'wait'),
            'done' if camera == 'connected' else ('run' if camera == 'connecting' else 'wait'),
            'done' if ready else 'wait',
        ]
        marks = {'done': ('√', theme.PALETTE['green']), 'run': ('→', theme.PALETTE['amber']),
                 'wait': ('○', '#777777')}
        base = ('① 扫描蓝牙广播', '② 连接心电贴', '③ 连接绿光手环',
                '④ 打开 USB 摄像头', '⑤ 设备就绪，可以开始采集')
        for label_widget, text, state in zip(self.step_labels, base, step_states):
            mark, color = marks[state]
            label_widget.setText('{}  {}'.format(mark, text))
            label_widget.setStyleSheet('padding: 3px 4px; color: {}; font-weight: {};'
                                       .format(color, 'bold' if state == 'done' else 'normal'))


class EcgPage(ChannelGridPage):
    title = '心电'
    subtitle = ''
    CHANNELS = [
        {'key': 'ecg', 'title': '心电波形', 'unit': 'ADC'},
        {'key': 'resp', 'title': '呼吸波形', 'unit': ''},
        {'key': 'accx', 'title': '三轴 X', 'unit': 'g'},
        {'key': 'accy', 'title': '三轴 Y', 'unit': 'g'},
        {'key': 'accz', 'title': '三轴 Z', 'unit': 'g'},
        {'key': 'temp', 'title': '体温趋势', 'unit': '℃'},
    ]
    DEFAULT_SELECTED = ['ecg', 'resp', 'accx', 'accy']

    def build_tiles(self) -> None:
        # 数值块只留指标名 + 单位；"峰值法估计"这类方法说明属于实现细节，不上界面
        self.tile_hr = StatTile('心率', 'bpm', '--', theme.PALETTE['red'])
        self.tile_temp = StatTile('体温', '℃', '--', theme.PALETTE['red'])
        self.tile_resp = StatTile('呼吸率', '次/分', '--', theme.PALETTE['red'])
        for tile in (self.tile_hr, self.tile_temp, self.tile_resp):
            self.tiles_row.addWidget(tile, 1)

    @staticmethod
    def _rates(snapshot: Dict[str, Any]) -> Tuple[float, float, float]:
        rates = snapshot.get('rates') or {}
        fs_ecg = rates.get('ecg_samples') or ECG_RATE
        if fs_ecg < 50:
            fs_ecg = ECG_RATE
        return fs_ecg, fs_ecg * RESP_RATE / ECG_RATE, fs_ecg * ECG_ACCEL_RATE / ECG_RATE

    def channel_data(self, snapshot: Dict[str, Any]):
        fs_ecg, fs_resp, fs_accel = self._rates(snapshot)
        ecg = snapshot['ecg']
        accel = snapshot['ecg_accel']
        temp = snapshot['temp']

        def last(values, digits=0):
            if values is None or len(values) == 0:
                return '--'
            return f'{values[-1]:.{digits}f}'

        return {
            'ecg': (ecg, fs_ecg, last(ecg)),
            'resp': (snapshot['resp'], fs_resp, last(snapshot['resp'], 1)),
            'accx': (accel['x'], fs_accel, last(accel['x'], 2)),
            'accy': (accel['y'], fs_accel, last(accel['y'], 2)),
            'accz': (accel['z'], fs_accel, last(accel['z'], 2)),
            'temp': (temp, max(1.0, (snapshot.get('rates') or {}).get('ecg_packets', 3.0)),
                     last(temp, 2)),
        }

    def channel_end_time(self, snapshot: Dict[str, Any], key: str) -> Optional[float]:
        """横轴用会话绝对秒数：心电/呼吸按采样点算，三轴与体温按包数算。"""
        counters = snapshot.get('counters') or {}
        fs_ecg, _fs_resp, fs_accel = self._rates(snapshot)
        packets = counters.get('ecg_packets', 0)
        if key in ('ecg', 'resp'):
            return counters.get('ecg_samples', 0) / fs_ecg
        if key in ('accx', 'accy', 'accz'):
            return packets * 16 / fs_accel          # 每包 16 个三轴点
        if key == 'temp':
            return packets * 80 / ECG_RATE          # 每包一个体温值（3.125 包/秒）
        return None

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        rates = snapshot.get('rates') or {}
        counters = snapshot['counters']
        fs_ecg, fs_resp, _fs_accel = self._rates(snapshot)
        ecg = np.asarray(snapshot['ecg'])
        quality, quality_text = ecg_signal_quality(ecg, fs_ecg)
        colors = {'ok': theme.PALETTE['green'], 'weak': theme.PALETTE['amber'],
                  'bad': theme.PALETTE['red'], 'wait': theme.PALETTE['text_faint']}
        color = colors.get(quality, theme.PALETTE['text_faint'])
        self.status_pill.setText(quality_text)
        self.status_pill.setStyleSheet(
            f'border-radius: 2px; padding: 3px 10px; font-size: {theme.pt(theme.FS_SMALL)};'
            f'background-color: #F7F7F7; border: 1px solid {color}; color: {color};')

        bpm = estimate_heart_rate(ecg, fs_ecg)
        # 有数就不挂方法论说明，没数才显示信号状态
        self.tile_hr.set_value(f'{bpm:.0f}' if bpm else '--',
                               note='' if bpm else quality_text)
        if snapshot['temp_last'] is not None:
            self.tile_temp.set_value(f'{snapshot["temp_last"]:.2f}')
        resp = np.asarray(snapshot['resp'])
        if resp_has_signal(resp):
            resp_rate = estimate_resp_rate(resp, fs_resp)
            if resp_rate:
                self.tile_resp.set_value(f'{resp_rate:.1f}')
        else:
            self.tile_resp.set_value('--', note='无呼吸数据')
        age = snapshot['ages'].get('ecg')
        state = '未连接' if age is None else ('接收中' if age < 3 else f'{age:.0f} 秒无数据')
        self.info_label.setText(
            f'状态 {state} · 心电包 {counters["ecg_packets"]} · 采样点 '
            f'{counters["ecg_samples"]}')


class BraceletPage(ChannelGridPage):
    title = '手环'
    subtitle = ''
    CHANNELS = [
        {'key': 'ppg1', 'title': 'PPG 通道 1', 'unit': 'ADC'},
        {'key': 'ppg2', 'title': 'PPG 通道 2', 'unit': 'ADC'},
        {'key': 'ppg3', 'title': 'PPG 通道 3', 'unit': 'ADC'},
        {'key': 'ppg4', 'title': 'PPG 通道 4', 'unit': 'ADC'},
        {'key': 'gsr', 'title': '皮肤电导 GSR', 'unit': ''},
        {'key': 'wx', 'title': '腕部三轴 X', 'unit': 'g'},
        {'key': 'wy', 'title': '腕部三轴 Y', 'unit': 'g'},
        {'key': 'wz', 'title': '腕部三轴 Z', 'unit': 'g'},
    ]
    DEFAULT_SELECTED = ['ppg1', 'ppg2', 'ppg3', 'ppg4']

    def build_tiles(self) -> None:
        self.tile_gsr = StatTile('GSR 均值', '', '--', theme.PALETTE['red'])
        self.tile_skin = StatTile('肤温', '℃', '--', theme.PALETTE['red'])
        self.tile_battery = StatTile('电量', '%', '--', theme.PALETTE['red'])
        for tile in (self.tile_gsr, self.tile_skin, self.tile_battery):
            self.tiles_row.addWidget(tile, 1)

    def channel_data(self, snapshot: Dict[str, Any]):
        rates = snapshot.get('rates') or {}
        ppg_rate = rates.get('ppg_points') or 130.0
        gsr_rate = rates.get('gsr') or 5.0
        bracelet_accel = snapshot['bracelet_accel']
        ppg = snapshot['ppg']
        window = max(8, int(ppg_rate * 0.8))            # 约 0.8 秒的滑动均值当基线

        def last(values, digits=0):
            if values is None or len(values) == 0:
                return '--'
            return f'{values[-1]:.{digits}f}'

        ppg_ac = {key: ac_couple(values, window) for key, values in ppg.items()}

        return {
            'ppg1': (ppg_ac['PPG1'], ppg_rate, last(ppg_ac['PPG1'], 1)),
            'ppg2': (ppg_ac['PPG2'], ppg_rate, last(ppg_ac['PPG2'], 1)),
            'ppg3': (ppg_ac['PPG3'], ppg_rate, last(ppg_ac['PPG3'], 1)),
            'ppg4': (ppg_ac['PPG4'], ppg_rate, last(ppg_ac['PPG4'], 1)),
            'gsr': (snapshot['gsr'], gsr_rate, last(snapshot['gsr'], 1)),
            'wx': (bracelet_accel['x'], max(1.0, ppg_rate / 10.0), last(bracelet_accel['x'], 2)),
            'wy': (bracelet_accel['y'], max(1.0, ppg_rate / 10.0), last(bracelet_accel['y'], 2)),
            'wz': (bracelet_accel['z'], max(1.0, ppg_rate / 10.0), last(bracelet_accel['z'], 2)),
        }

    def channel_end_time(self, snapshot: Dict[str, Any], key: str) -> Optional[float]:
        """横轴用会话绝对秒数（PPG 按采样点、GSR 按样本、腕部三轴按包）。"""
        counters = snapshot.get('counters') or {}
        rates = snapshot.get('rates') or {}
        ppg_rate = rates.get('ppg_points') or 250.0
        gsr_rate = rates.get('gsr') or 10.0
        if key.startswith('ppg'):
            return counters.get('ppg_samples', 0) / ppg_rate
        if key == 'gsr':
            return counters.get('gsr_samples', 0) / gsr_rate
        if key in ('wx', 'wy', 'wz'):
            return counters.get('bracelet_packets', 0) / 25.0    # 手环 25 包/秒
        return None

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        rates = snapshot.get('rates') or {}
        counters = snapshot['counters']
        if snapshot['gsr']:
            gsr = np.asarray(snapshot['gsr'], dtype=float)
            if gsr.max() - gsr.min() < 1e-6 and abs(gsr.mean()) < 1e-6:
                self.tile_gsr.set_value('--', note='GSR 无信号（未接触）')
            else:
                self.tile_gsr.set_value(f'{gsr.mean():.1f}')
        if snapshot['skin_temp'] is not None:
            self.tile_skin.set_value(f'{snapshot["skin_temp"]:.2f}')
        battery = snapshot.get('battery') or {}
        if battery.get('percent') is not None:
            self.tile_battery.set_value(f'{battery["percent"]:.0f}')
        age = snapshot['ages'].get('bracelet')
        state = '未连接' if age is None else ('接收中' if age < 3 else f'{age:.0f} 秒无数据')
        color = theme.PALETTE['green'] if age is not None and age < 3 else theme.PALETTE['amber']
        self.status_pill.setText(f'手环{state}')
        self.status_pill.setStyleSheet(
            f'border-radius: 2px; padding: 3px 10px; font-size: {theme.pt(theme.FS_SMALL)};'
            f'background-color: #F7F7F7; border: 1px solid {color}; color: {color};')
        self.info_label.setText(
            f'状态 {state} · 手环包 {counters["bracelet_packets"]} · PPG 包 '
            f'{counters["ppg_packets"]} · GSR 包 {counters["gsr_packets"]}')


class StatusPage(BasePage):
    title = '会话与回放'
    subtitle = '选择历史会话回放，或删除不需要的会话库'

    # 回放内容 key -> (标题, 单位, [(曲线名, 颜色), ...])
    # 每一项对应右边一块折线图；勾选哪几项就生成哪几块。
    # key 与 ui/replay.py 的 REPLAY_SOURCES 一致，摄像头单独占左边大版面，不在这里。
    REPLAY_CHARTS: Dict[str, Tuple[str, str, List[Tuple[str, str]]]] = {
        'ecg':     ('心电',      'ADC', [('ECG', '#00D0FF')]),
        'resp':    ('呼吸',      'ADC', [('RESP', '#FFD700')]),
        'accel':   ('三轴',      'g',   [('X', '#FF5252'), ('Y', '#00E676'), ('Z', '#00D0FF')]),
        'temp':    ('体温',      '℃',   [('体温', '#FF80AB')]),
        'ppg':     ('PPG',       'ADC', [('PPG1', '#00E676')]),
        'b_accel': ('腕部三轴',   'g',   [('X', '#FF5252'), ('Y', '#00E676'), ('Z', '#00D0FF')]),
        'gsr':     ('皮电 GSR',  'ADC', [('GSR', '#B388FF')]),
        'b_temp':  ('肤温',      '℃',   [('肤温', '#FFD700')]),
        'battery': ('电量',      '%',   [('电量', '#00D0FF')]),
    }

    def build(self) -> None:
        body = QHBoxLayout()
        body.setSpacing(8)
        left = QVBoxLayout()
        left.setSpacing(8)

        # 数据源卡片只留会话信息：心电贴/手环的连接状态在「采集」段已经显示过一遍，
        # 回放时再放一份只会占地方（而且回放根本没有"连接"这件事）。
        source_card = Card('数据源')
        self.session_label = mono('—')
        self.session_label.setWordWrap(True)
        source_card.add(self.session_label)
        left.addWidget(source_card)

        stats_card = Card('数据统计')
        self.stats_table = QTableWidget(0, 2)
        self.stats_table.setHorizontalHeaderLabels(['项目', '值'])
        self.stats_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.stats_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.stats_table.verticalHeader().setVisible(False)
        self.stats_table.setMinimumHeight(180)
        self.stats_table.setMaximumHeight(theme.scaled(300))
        stats_card.add(self.stats_table)
        left.addWidget(stats_card, 1)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        replay_card = Card('回放控制')
        self.replay_combo = QComboBox()
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self._refresh_sessions)
        self.delete_button = QPushButton('删除')
        self.delete_button.setObjectName('Danger')
        self.delete_button.clicked.connect(self._delete_session)
        row = QHBoxLayout()
        row.addWidget(self.replay_combo, 1)
        row.addWidget(refresh)
        row.addWidget(self.delete_button)
        replay_card.add_layout(row)
        row2 = QHBoxLayout()
        row2.addWidget(label('倍速', 'Dim'))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.25, 50.0)
        self.speed_spin.setValue(2.0)
        self.speed_spin.setFixedWidth(80)
        row2.addWidget(self.speed_spin)
        self.replay_button = QPushButton('开始回放')
        self.replay_button.setObjectName('Primary')
        self.replay_button.clicked.connect(self._toggle_replay)
        row2.addWidget(self.replay_button)
        row2.addStretch(1)
        replay_card.add_layout(row2)

        # 回放内容：把选中的会话库里实际有数据的表列出来，逐项勾选。
        # 空表不列（勾了也没内容，反而让人以为坏了）。
        pick_row = QHBoxLayout()
        pick_row.addWidget(label('回放内容', 'Dim'), 0)
        self.source_all = QPushButton('全选')
        self.source_all.setFixedWidth(theme.scaled(56))
        self.source_all.clicked.connect(lambda: self._check_all_sources(True))
        self.source_none = QPushButton('全不选')
        self.source_none.setFixedWidth(theme.scaled(64))
        self.source_none.clicked.connect(lambda: self._check_all_sources(False))
        pick_row.addStretch(1)
        pick_row.addWidget(self.source_all)
        pick_row.addWidget(self.source_none)
        replay_card.add_layout(pick_row)

        self.source_list = QListWidget()
        self.source_list.setMaximumHeight(theme.scaled(132))
        self.source_list.setAlternatingRowColors(True)
        replay_card.add(self.source_list)
        self.source_hint = mono('先选会话')
        self.source_hint.setWordWrap(True)
        replay_card.add(self.source_hint)
        self.replay_combo.currentIndexChanged.connect(self._refresh_sources)
        self.source_list.itemChanged.connect(self._on_source_check_changed)
        self.source_list.itemClicked.connect(self._focus_source)

        # 时间轴：每条设备一条"在线时段"横条，上面压一个可拖拽的区间选择。
        # 原来的只读进度条只能看不能选，所以没法回答"我要分析哪一段"。
        self.timeline = pg.PlotWidget(background=theme.PALETTE['plot_bg'])
        self.timeline.setFixedHeight(theme.scaled(92))   # 固定住，否则被布局拉成 400+
        self.timeline.setMouseEnabled(x=False, y=False)
        self.timeline.hideButtons()
        self.timeline.getAxis('left').setTicks([[]])
        self.timeline.getAxis('bottom').setLabel('会话内秒数')
        tick_font = QFont(theme.FONT_MONO)
        tick_font.setPointSizeF(theme.scaled_pt(theme.PLOT_TICK_PT))
        for axis in ('left', 'bottom'):
            ax = self.timeline.getAxis(axis)
            ax.setStyle(tickFont=tick_font)
            ax.setPen(pg.mkPen('#808080'))
            ax.setTextPen(pg.mkPen(theme.PALETTE['plot_text']))
        replay_card.add(self.timeline)
        self._tl_items: List[Any] = []
        self._region_touched = False      # 用户是否真的动过选区/点过自动选区
        self._tl_origin = 0.0
        self._tl_duration = 0.0
        self.selection = pg.LinearRegionItem(values=(0, 1), movable=True,
                                             brush=pg.mkBrush(46, 109, 180, 40),
                                             pen=pg.mkPen('#2E6DB4', width=2))
        self.selection.setZValue(20)
        self.timeline.addItem(self.selection)
        self.selection.sigRegionChangeFinished.connect(self._on_region_dragged)
        self.play_cursor = pg.InfiniteLine(pos=0, angle=90,
                                           pen=pg.mkPen('#FFD700', width=2))
        self.play_cursor.setZValue(30)
        self.timeline.addItem(self.play_cursor)

        tl_row = QHBoxLayout()
        self.auto_pick = QPushButton('自动选区')
        self.auto_pick.setToolTip('自动选中所有设备同时在线的那一段')
        self.auto_pick.clicked.connect(self._auto_pick_overlap)
        self.to_analysis = QPushButton('进入分析')
        self.to_analysis.setObjectName('Primary')
        self.to_analysis.clicked.connect(self._goto_analysis)
        tl_row.addWidget(self.auto_pick)
        tl_row.addWidget(self.to_analysis)
        tl_row.addStretch(1)
        replay_card.add_layout(tl_row)

        self.selection_text = mono('先在左上方选一个会话')
        self.selection_text.setWordWrap(True)
        replay_card.add(self.selection_text)
        self.replay_info = mono('选择会话文件（.db / .jsonl）')
        self.replay_info.setWordWrap(True)
        replay_card.add(self.replay_info)
        replay_card.body.addStretch(1)   # 控件靠上排，别被卡片高度均匀拉开
        right.addWidget(replay_card)

        body.addLayout(right, 1)
        self.content.addLayout(body)

        # ---- 回放预览区 ----
        # 左边摄像头独占一块大版面（回放时的画面就是靠它看细节），
        # 右边按「回放内容」里勾选的表动态铺折线图：勾一个出一条，取消就收掉。
        preview_row = QHBoxLayout()
        preview_row.setSpacing(8)

        self.camera_card = Card('回放画面')
        self.preview_camera = QLabel('无摄像头帧')
        self.preview_camera.setAlignment(Qt.AlignCenter)
        self.preview_camera.setMinimumSize(theme.scaled(360), theme.scaled(200))
        self.preview_camera.setStyleSheet(
            'background-color: #101010; color: #C8C8C8; border: 1px solid #808080;')
        self.camera_card.add(self.preview_camera, 1)
        self.camera_card.setMinimumHeight(theme.scaled(220))
        preview_row.addWidget(self.camera_card, 5)

        self.chart_host = QWidget()
        self.chart_grid = QGridLayout(self.chart_host)
        self.chart_grid.setContentsMargins(0, 0, 0, 0)
        self.chart_grid.setSpacing(8)
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll.setWidget(self.chart_host)
        self.chart_scroll.setMinimumHeight(theme.scaled(240))
        preview_row.addWidget(self.chart_scroll, 6)

        self.content.addLayout(preview_row, 1)
        self._charts: Dict[str, ChartPanel] = {}
        self._active_source = ''
        self._scalar_hist = {'b_temp': deque(maxlen=900), 'battery': deque(maxlen=900)}
        self._refresh_sessions()

    def on_data(self, snapshot: Dict[str, Any]) -> None:
        """回放预览：按勾选的内容，把每一路数据画进各自的图里。"""
        rates = snapshot.get('rates') or {}
        counters = snapshot.get('counters') or {}

        fs_ecg = rates.get('ecg_samples') or ECG_RATE
        if fs_ecg < 50:
            fs_ecg = ECG_RATE
        end_ecg = counters.get('ecg_samples', 0) / fs_ecg
        ppg_rate = rates.get('ppg_points') or 130.0
        end_ppg = counters.get('ppg_samples', 0) / ppg_rate
        gsr_rate = rates.get('gsr') or 5.0
        end_gsr = counters.get('gsr_samples', 0) / gsr_rate
        b_accel_rate = rates.get('bracelet_accel') or 10.0

        for key, panel in self._charts.items():
            try:
                if key == 'ecg':
                    ecg = snapshot['ecg']
                    panel.set_series('ECG', ecg, fs_ecg, end_time=end_ecg)
                    panel.set_value('{:,.0f} 点'.format(float(np.size(ecg))))
                elif key == 'resp':
                    panel.set_series('RESP', snapshot['resp'], RESP_RATE, end_time=end_ecg)
                elif key == 'accel':
                    # 缓冲里的三轴键是小写 x/y/z（见 ui/buffers.py），图例仍用大写
                    accel = snapshot.get('ecg_accel') or {}
                    for axis in ('X', 'Y', 'Z'):
                        panel.set_series(axis, accel.get(axis.lower(), ()), ECG_ACCEL_RATE,
                                         end_time=end_ecg)
                elif key == 'temp':
                    panel.set_series('体温', snapshot['temp'], 1.0, end_time=end_ecg)
                elif key == 'ppg':
                    ppg = snapshot['ppg']['PPG1']
                    panel.set_series('PPG1', ac_couple(ppg, max(8, int(ppg_rate * 0.8))),
                                     ppg_rate, end_time=end_ppg)
                elif key == 'b_accel':
                    accel = snapshot.get('bracelet_accel') or {}
                    for axis in ('X', 'Y', 'Z'):
                        panel.set_series(axis, accel.get(axis.lower(), ()), b_accel_rate,
                                         end_time=end_ppg)
                elif key == 'gsr':
                    panel.set_series('GSR', snapshot['gsr'], gsr_rate, end_time=end_gsr)
                elif key in ('b_temp', 'battery'):
                    self._push_scalar(key, snapshot, panel)
            except Exception:                             # noqa: BLE001 - 单图出错不拖垮整页
                continue

        self._update_camera_preview(snapshot)

    def _push_scalar(self, key: str, snapshot: Dict[str, Any], panel) -> None:
        """肤温 / 电量是单值，没有历史数组，这里按帧累积成一条线。"""
        if key == 'b_temp':
            value = snapshot.get('skin_temp')
            curve, unit = '肤温', '℃'
        else:
            battery = snapshot.get('battery') or {}
            value = battery.get('percent') or battery.get('电量')
            curve, unit = '电量', '%'
        if value is None:
            return
        hist = self._scalar_hist[key]
        hist.append(float(value))
        series = list(hist)
        panel.set_series(curve, series, 1.0, end_time=float(len(series)))
        panel.set_value('{:.1f}{}'.format(series[-1], unit))

    def _update_camera_preview(self, snapshot: Dict[str, Any]) -> None:
        """回放（或实时）画面：优先用回放流里的帧，没有就用正在采集的摄像头帧。"""
        import cv2
        import numpy as np

        frame = snapshot.get('camera_frame')
        if frame is None and self.window_ref.camera_service is not None:
            frame = self.window_ref.camera_service.latest_frame()
        if not frame:
            return
        jpeg, index, _stamp = frame
        now = time.time()
        if index == getattr(self, '_camera_index', None) and \
                now - getattr(self, '_camera_at', 0.0) < 0.15:
            return
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return
        self._camera_index = index
        self._camera_at = now
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        height, width, _ = rgb.shape
        qimage = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888).copy()
        self.preview_camera.setPixmap(QPixmap.fromImage(qimage).scaled(
            max(64, self.preview_camera.width() - 4),
            max(64, self.preview_camera.height() - 4),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    @staticmethod
    def _path_key(path) -> str:
        try:
            return os.path.normcase(os.path.abspath(str(path)))
        except Exception:                             # noqa: BLE001
            return str(path)

    def _find_session_index(self, path) -> int:
        key = self._path_key(path)
        for index in range(self.replay_combo.count()):
            if self._path_key(self.replay_combo.itemData(index)) == key:
                return index
        return -1

    def sync_session(self, path: str) -> None:
        """把下拉框切到"正在回放的库"。

        时间轴、回放内容、预览曲线都以下拉框选中的会话为准。如果不跟着走，
        命令行 `--replay` 或外部调用 start_replay 时，界面显示的会是另一个库的
        时间轴，看起来像"图例对不上"。
        """
        if not path:
            return
        index = self._find_session_index(path)
        if index < 0:
            self.replay_combo.addItem(
                '{}（正在回放）'.format(os.path.basename(path)), path)
            index = self.replay_combo.count() - 1
        if index != self.replay_combo.currentIndex():
            self.replay_combo.setCurrentIndex(index)      # 会自动刷新时间轴与内容列表
        else:
            self._refresh_sources()

    def _refresh_sessions(self) -> None:
        """重建会话下拉框，**保留当前选择**。

        以前这里直接 clear() 再填，等于每次刷新都把选择重置成"最新那个库"。
        进入第②段就会刷新一次，于是时间轴会莫名其妙变成另一个会话 ——
        正在回放 A，界面显示的却是 B。
        """
        current = self.replay_combo.currentData()
        self.replay_combo.blockSignals(True)
        self.replay_combo.clear()
        # 用 DATA_DIR 而不是相对路径 'data'：从别的目录启动时列表不会变空
        for path, mtime, size in list_sessions(DATA_DIR):
            stamp = datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')
            self.replay_combo.addItem(
                f'{os.path.basename(path)}   {stamp}   {format_size(size)}', path)
        index = self._find_session_index(current) if current else -1
        if index < 0 and current:
            # 选中的库不在 data/ 下（比如命令行指定的外部路径），补一项保住它
            self.replay_combo.addItem(
                '{}（当前选中）'.format(os.path.basename(str(current))), current)
            index = self.replay_combo.count() - 1
        self.replay_combo.setCurrentIndex(index if index >= 0 else 0)
        self.replay_combo.blockSignals(False)
        self._refresh_sources()

    # ------------------------------------------------- 回放内容（表选择）
    def _refresh_sources(self) -> None:
        """按当前选中的会话库，把里面**有数据**的表列成可勾选项。"""
        self.source_list.blockSignals(True)
        self.source_list.clear()
        self._refresh_timeline()
        path = self.replay_combo.currentData()
        if not path:
            self.source_list.blockSignals(False)
            self.source_hint.setText('先选会话')
            self._sync_charts()
            return
        if str(path).lower().endswith('.jsonl'):
            item = QListWidgetItem('心电 · JSONL 每行一包')
            item.setData(Qt.UserRole, 'ecg')
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.source_list.addItem(item)
            self.source_list.blockSignals(False)
            self.source_hint.setText('JSONL 只含心电包')
            self._sync_charts()
            return
        try:
            sources = list_db_sources(path)
        except Exception as exc:                          # noqa: BLE001 - 坏库不该拖垮页面
            self.source_list.blockSignals(False)
            self.source_hint.setText('读取失败：{}'.format(exc))
            self._sync_charts()
            return
        if not sources:
            self.source_list.blockSignals(False)
            self.source_hint.setText('这个会话没有任何数据表')
            self._sync_charts()
            return
        for key, group, name, count in sources:
            item = QListWidgetItem('{:<4} {:<8} {:>9,} 行'.format(group, name, count))
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.source_list.addItem(item)
        self.source_list.blockSignals(False)
        self._update_source_hint()
        self._sync_charts()

    def _on_source_check_changed(self, _item) -> None:
        self._update_source_hint()
        self._sync_charts()

    def _update_source_hint(self) -> None:
        total = self.source_list.count()
        picked = len(self._selected_sources())
        self.source_hint.setText('已选 {} / {} 项'.format(picked, total))

    def _check_all_sources(self, checked: bool) -> None:
        self.source_list.blockSignals(True)          # 批量勾选，别触发 N 次重建
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.source_list.count()):
            self.source_list.item(index).setCheckState(state)
        self.source_list.blockSignals(False)
        self._update_source_hint()
        self._sync_charts()                          # 信号被屏蔽了，这里必须手动同步一次

    def _selected_sources(self) -> List[str]:
        keys: List[str] = []
        for index in range(self.source_list.count()):
            item = self.source_list.item(index)
            if item.checkState() == Qt.Checked:
                keys.append(item.data(Qt.UserRole))
        return keys

    # ------------------------------------------------- 图表与勾选联动
    def _sync_charts(self) -> None:
        """按勾选情况重建右侧折线图：勾一个出一条，取消就收掉。"""
        want = [key for key in self._selected_sources() if key in self.REPLAY_CHARTS]
        if want == list(self._charts.keys()):
            return
        while self.chart_grid.count():
            old_item = self.chart_grid.takeAt(0)
            old_widget = old_item.widget() if old_item is not None else None
            if old_widget is not None:
                old_widget.setParent(None)
                old_widget.deleteLater()
        self._charts.clear()
        for row in range(self.chart_grid.rowCount()):
            self.chart_grid.setRowStretch(row, 0)
        for col in range(2):
            self.chart_grid.setColumnStretch(col, 1)
        for index, key in enumerate(want):
            title, unit, curves = self.REPLAY_CHARTS[key]
            panel = ChartPanel(title, unit)
            for name, color in curves:
                panel.add_curve(name, color, 1.3)
            panel.plot.setMinimumHeight(theme.scaled(88))
            self._charts[key] = panel
            self.chart_grid.addWidget(panel, index // 2, index % 2)
        if not want:
            empty = label('未勾选任何可绘制的数据（摄像头画面在左侧）', 'CardHint')
            empty.setAlignment(Qt.AlignCenter)
            self.chart_grid.addWidget(empty, 0, 0, 1, 2)
            self._apply_active()
            return
        # 最后一行吃掉剩余高度，图不会被拉成等高的大块空白
        tail_row = (len(want) - 1) // 2 + 1
        self.chart_grid.setRowStretch(tail_row, 1)
        self._apply_active()

    def _apply_active(self) -> None:
        """把当前点选的那一路标出来：图标题前加 ▶，摄像头加蓝框。"""
        for key, panel in self._charts.items():
            title = self.REPLAY_CHARTS[key][0]
            panel.set_title(('▶ ' if key == self._active_source else '') + title)
        is_camera = self._active_source == 'camera'
        self.preview_camera.setStyleSheet(
            'background-color: #101010; color: #C8C8C8; border: {};'.format(
                '2px solid #2E6DB4' if is_camera else '1px solid #808080'))

    def _focus_source(self, item) -> None:
        """点一行就把对应的图/画面标出来。"""
        self._active_source = item.data(Qt.UserRole) or ''
        self._apply_active()
        if self._active_source in self._charts:
            # 图多的时候把它滚进可见区，否则点了看不到反应
            self.chart_scroll.ensureWidgetVisible(self._charts[self._active_source])

    # ------------------------------------------------- 时间轴与区间选择
    def _refresh_timeline(self) -> None:
        """画出每台设备的在线时段，并把选区重置为整段。"""
        if not hasattr(self, 'timeline'):
            return
        for item in self._tl_items:
            self.timeline.removeItem(item)
        self._tl_items.clear()
        self._tl_origin, self._tl_duration = 0.0, 0.0
        self._region_touched = False
        path = self.replay_combo.currentData()
        self.play_cursor.setPos(0)
        if not path or str(path).lower().endswith('.jsonl'):
            self.timeline.setXRange(0, 1, padding=0)
            self.timeline.getAxis('left').setTicks([[]])
            self.selection.hide()
            return
        spans = instrument_spans(path)
        if not spans:
            self.timeline.setXRange(0, 1, padding=0)
            self.timeline.getAxis('left').setTicks([[]])
            self.selection.hide()
            self.selection_text.setText('这个会话没有可用的数据表')
            return
        origin = min(item['start'] for item in spans)
        end = max(item['end'] for item in spans)
        self._tl_origin = origin
        self._tl_duration = max(1.0, end - origin)
        colors = {'ecg': '#2E6DB4', 'bracelet': '#2E8B57', 'camera': '#B8860B'}
        ticks = []
        for row, span in enumerate(spans):
            color = colors.get(span['key'], '#7F7F7F')
            bar = pg.PlotDataItem([span['start'] - origin, span['end'] - origin],
                                  [row, row], pen=pg.mkPen(color, width=8))
            self.timeline.addItem(bar)
            self._tl_items.append(bar)
            ticks.append((row, '{} {:.0f}s'.format(span['name'],
                                                   span['end'] - span['start'])))
        # 设备名放在左侧刻度上，而不是画进图里 —— 画进图里会被左边界裁掉一截，
        # 而且设备多的时候文字会和横条叠在一起。
        left = self.timeline.getAxis('left')
        left.setTicks([ticks])
        left.setWidth(theme.scaled(86))
        self.timeline.setYRange(-0.8, len(spans) - 0.2, padding=0)
        self.timeline.setXRange(0, self._tl_duration, padding=0.02)
        self.selection.show()
        self.selection.setRegion((0.0, self._tl_duration))
        self._on_selection_changed()

    def _on_region_dragged(self) -> None:
        self._region_touched = True
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        if self._tl_duration <= 0:
            return
        start, end = self.selection.getRegion()
        span = max(0.0, end - start)
        self.selection_text.setText('已选 {:.1f} ~ {:.1f} s，共 {:.1f} s（占 {:.0f}%）'.format(
            start, end, span, span * 100.0 / self._tl_duration))

    def _auto_pick_overlap(self) -> None:
        """一键选中所有设备同时在线的那一段（按设备算，不按表算）。"""
        path = self.replay_combo.currentData()
        if not path:
            self.selection_text.setText('先选一个会话')
            return
        result = overlap_window(path)
        if not result['ok']:
            missing = '、'.join(result['missing']) or '任意设备'
            self.selection_text.setText(
                '没有全部设备同时在线的时段（缺：{}）。可手动拖选区，或只分析已连上的设备'
                .format(missing))
            return
        start = result['start'] - self._tl_origin
        end = result['end'] - self._tl_origin
        self._region_touched = True
        self.selection.setRegion((start, end))
        self.selection_text.setText(
            '自动选区：{:.1f} ~ {:.1f} s，共 {:.1f} s（{} 台设备同时在线的交集）'.format(
                start, end, end - start, len(result['devices'])))

    def confirm_selection(self) -> bool:
        """校验选区并进入分析段。「进入分析」按钮与底部「下一步」共用这一个入口。"""
        path = self.replay_combo.currentData()
        if not path:
            self.selection_text.setText('先选一个会话')
            return False
        start, end = self.selection.getRegion()
        if not self._region_touched:
            # 默认就是整段 + 已选会话，如果这里放行，"下一步"等于没做任何选择也能过
            self.selection_text.setText('请先拖出要分析的区间，或点「自动选区」')
            return False
        if end - start < 0.5:
            self.selection_text.setText('选区太短（不足 0.5 秒），把两个把手拖宽一点')
            return False
        window = self.window_ref
        window.review_selection = {'path': path,
                                   'start': self._tl_origin + start,
                                   'end': self._tl_origin + end}
        window.mark_stage_done(1)          # 必须先解锁，否则 goto_stage(2) 会被门拦下
        page = getattr(window, 'analysis_page', None)
        if page is not None and hasattr(page, 'adopt_selection'):
            page.adopt_selection()
        window.goto_stage(2)
        return True

    def _goto_analysis(self) -> None:
        """回放页里的「进入分析」按钮。"""
        self.confirm_selection()

    def _delete_session(self) -> None:
        """删除当前选中的会话库（连同 -wal / -shm）：双确认，删前把文件名和大小写清楚。"""
        path = self.replay_combo.currentData()
        if not path or not os.path.exists(path):
            self.replay_info.setText('没有可删除的文件')
            return
        if self.window_ref.replay is not None:
            self.window_ref.stop_replay()          # 正在回放的库不能删

        size = format_size(os.path.getsize(path))
        answer = QMessageBox.question(
            self, '确认删除',
            '将永久删除这个会话库，无法恢复：\n\n{}\n{}\n\n'
            '（同目录的 -wal / -shm 一并删除）'.format(os.path.basename(path), size),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            self.replay_info.setText('已取消删除')
            return
        removed = 0
        for suffix in ('', '-wal', '-shm', '-journal'):
            target = path + suffix
            if os.path.exists(target):
                try:
                    os.remove(target)
                    removed += 1
                except OSError as exc:
                    QMessageBox.warning(self, '删除失败', f'{target}\n{exc}')
                    break
        self._refresh_sessions()
        self.replay_info.setText('已删除 {} 个文件：{}'.format(removed, os.path.basename(path)))

    def _toggle_replay(self) -> None:
        window = self.window_ref
        if window.replay is not None:
            window.stop_replay()
            self.replay_button.setText('开始回放')
            self.replay_button.setObjectName('Primary')
            self.replay_button.setStyle(self.replay_button.style())
            self.replay_info.setText('已停止回放')
            return
        path = self.replay_combo.currentData()
        if not path:
            self.replay_info.setText('没有可回放的文件')
            return
        include = self._selected_sources()
        if not include:
            self.replay_info.setText('请至少勾选一项「回放内容」')
            return
        count = window.start_replay(path, self.speed_spin.value(), include=include)
        self.replay_button.setText('停止回放')
        self.replay_button.setObjectName('Danger')
        self.replay_button.setStyle(self.replay_button.style())
        picked = len(include)
        self.replay_info.setText(
            f'回放 {os.path.basename(path)} · {count} 包 · '
            f'{self.speed_spin.value():g}x · 已选 {picked} 项')

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        counters = snapshot['counters']
        ages = snapshot['ages']
        rows = [
            ('心电包', counters['ecg_packets']),
            ('心电采样点', counters['ecg_samples']),
            ('手环包', counters['bracelet_packets']),
            ('PPG 采样点', counters['ppg_samples']),
            ('GSR 包', counters['gsr_packets']),
        ]
        self.stats_table.setRowCount(len(rows))
        for index, (key, value) in enumerate(rows):
            self.stats_table.setItem(index, 0, QTableWidgetItem(key))
            self.stats_table.setItem(index, 1, QTableWidgetItem(str(value)))
        self.session_label.setText(f'库文件：{self.window_ref.session_info.get("db_path", "—")}\n'
                                   f'数据源：{self.window_ref.session_info.get("source", "—")}')

        player = self.window_ref.replay
        running = player is not None and player.is_alive()
        if running:
            position, total, fraction = player.progress()
            self.play_cursor.setPos(fraction * max(self._tl_duration, 0.001))
            source = self.window_ref.session_info.get('source', '回放')
            self.replay_info.setText('{} · 第 {}/{} 包 · 倍速 {:g}x'.format(
                source, position + 1, total, player.speed))
            want_text, want_style = '停止回放', 'Danger'
        else:
            if player is not None:
                self.window_ref.stop_replay()          # 自然放完
                self.play_cursor.setPos(max(self._tl_duration, 0.001))
                self.replay_info.setText('回放结束')
            want_text, want_style = '开始回放', 'Primary'
        if self.replay_button.text() != want_text:
            self.replay_button.setText(want_text)
            self.replay_button.setObjectName(want_style)
            self.replay_button.setStyle(self.replay_button.style())


class CameraPage(BasePage):
    """USB 摄像头：实时预览 + 30fps 截帧入库。"""

    title = '摄像头'
    subtitle = ''

    def build(self) -> None:
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tile_frames = StatTile('已入库帧', '帧', '0', theme.PALETTE['red'])
        self.tile_size = StatTile('库大小', 'MB', '0.0', theme.PALETTE['red'])
        self.tile_fatigue = StatTile('疲劳状态', '', '—', theme.PALETTE['text_dim'])
        self.tile_yawn = StatTile('哈欠次数', '次', '0', theme.PALETTE['red'])
        for tile in (self.tile_frames, self.tile_size, self.tile_fatigue, self.tile_yawn):
            tiles.addWidget(tile, 1)
        self.status_pill = pill('未开始', theme.PALETTE['text_dim'], '#F0F0F0', '#B0B0B0')
        tiles.addWidget(self.status_pill, 0)
        self.content.addLayout(tiles)

        body = QHBoxLayout()
        body.setSpacing(8)

        preview_card = Card('实时预览')
        self.preview = QLabel('摄像头未启动')
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(420)
        self.preview.setStyleSheet('background-color: #101010; color: #C8C8C8;'
                                   f' border: 1px solid #808080; font-size: {theme.pt(theme.FS_NAV)};')
        preview_card.add(self.preview, 1)
        self.frame_label = mono('—')
        preview_card.add(self.frame_label)
        body.addWidget(preview_card, 3)

        settings = Card('采集设置')
        settings.setFixedWidth(theme.scaled(300))

        def field_row(text: str) -> QHBoxLayout:
            """一行「标签 + 控件」。标签定宽，保证四个输入框左边缘对齐。"""
            layout = QHBoxLayout()
            name = label(text, 'Dim')
            name.setFixedWidth(theme.scaled(74))
            layout.addWidget(name)
            return layout

        row = field_row('设备号')
        self.device_spin = QDoubleSpinBox()
        self.device_spin.setDecimals(0)
        self.device_spin.setRange(0, 9)
        self.device_spin.setValue(0)
        self.device_spin.setFixedWidth(70)
        row.addWidget(self.device_spin)
        row.addStretch(1)
        settings.add_layout(row)

        row2 = field_row('分辨率')
        self.size_combo = QComboBox()
        for text, (w, h) in (('640×480', (640, 480)), ('1280×720', (1280, 720)),
                             ('1920×1080', (1920, 1080))):
            self.size_combo.addItem(text, (w, h))
        self.size_combo.setCurrentIndex(1)
        row2.addWidget(self.size_combo, 1)
        settings.add_layout(row2)

        row3 = field_row('目标帧率')
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 60.0)
        self.fps_spin.setValue(30.0)
        self.fps_spin.setFixedWidth(80)
        row3.addWidget(self.fps_spin)
        row3.addWidget(label('fps', 'Dim'))
        row3.addStretch(1)
        settings.add_layout(row3)

        row4 = field_row('JPEG 质量')
        self.quality_spin = QDoubleSpinBox()
        self.quality_spin.setRange(30, 100)
        self.quality_spin.setValue(80)
        self.quality_spin.setFixedWidth(80)
        row4.addWidget(self.quality_spin)
        row4.addStretch(1)
        settings.add_layout(row4)

        buttons = QHBoxLayout()
        self.start_button = QPushButton('开始采集')
        self.start_button.setObjectName('Primary')
        self.start_button.clicked.connect(self._toggle)
        buttons.addWidget(self.start_button)
        open_dir = QPushButton('打开数据库目录')
        open_dir.clicked.connect(self._open_dir)
        buttons.addWidget(open_dir)
        settings.add_layout(buttons)

        # 打哈欠检测开关：默认状态由 config.ini 的 [vision] enabled 决定（开箱为开启）。
        # 模型在「开始采集」之后才加载，避免没开摄像头就先占显存。
        yawn_row = QHBoxLayout()
        self.yawn_check = QCheckBox('启用打哈欠检测')
        self.yawn_check.toggled.connect(self._on_yawn_toggled)
        yawn_row.addWidget(self.yawn_check)
        yawn_row.addStretch(1)
        settings.add_layout(yawn_row)
        self.yawn_hint = mono('未启用')
        self.yawn_hint.setWordWrap(True)
        settings.add(self.yawn_hint)
        if getattr(Config.load(), 'vision_enabled', False):
            self.yawn_check.setChecked(True)      # 触发 _on_yawn_toggled，提示「开始采集后生效」

        settings.body.addStretch(1)      # 设置项靠上，库路径留在底部，不被均匀拉开
        self.db_label = mono('未开始')
        self.db_label.setWordWrap(True)
        settings.add(self.db_label)
        body.addWidget(settings, 1)
        self.content.addLayout(body, 1)

        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._refresh_preview)
        self.preview_timer.start(33)                  # 约 30fps 刷新预览

    # ------------------------------------------------------------- 控制
    def _toggle(self) -> None:
        window = self.window_ref
        if window.camera_service is not None and window.camera_service.running:
            window.stop_camera()
            self.start_button.setText('开始采集')
            self.start_button.setObjectName('Primary')
            self.start_button.setStyle(self.start_button.style())
            self.status_pill.setText('已停止')
            return
        width, height = self.size_combo.currentData()
        config = {
            'index': int(self.device_spin.value()),
            'width': width,
            'height': height,
            'fps': float(self.fps_spin.value()),
            'quality': int(self.quality_spin.value()),
            'backend': 'dshow',
        }
        try:
            db_path = window.start_camera(config)
        except Exception as exc:                      # noqa: BLE001
            self.status_pill.setText('启动失败')
            self.db_label.setText(str(exc))
            return
        self.db_label.setText('库：' + db_path)
        self.start_button.setText('停止采集')
        self.start_button.setObjectName('Danger')
        self.start_button.setStyle(self.start_button.style())
        self.status_pill.setText('采集中')
        # 勾了「启用打哈欠检测」就在采集启动后加载模型（首次约 2~5 秒）
        if self.yawn_check.isChecked():
            self._on_yawn_toggled(True)

    def _on_yawn_toggled(self, checked: bool) -> None:
        window = self.window_ref
        window.yawn_wanted = bool(checked)      # 记住用户意图，重启采集时沿用
        if not checked:
            window.set_yawn_enabled(False)
            self.yawn_hint.setText('未启用')
            return
        if window.camera_service is None or not window.camera_service.running:
            self.yawn_hint.setText('开始采集后生效')
            return
        self.yawn_hint.setText('模型加载中…')
        if window.set_yawn_enabled(True):
            self.yawn_hint.setText('正在初始化')
        else:
            self.yawn_hint.setText('启动失败：请先开始采集')

    def _open_dir(self) -> None:
        try:
            os.startfile('data')
        except Exception:                             # noqa: BLE001
            pass

    # ------------------------------------------------------------- 刷新
    def _refresh_preview(self) -> None:
        import cv2
        import numpy as np

        service = self.window_ref.camera_service
        if service is None:
            return
        latest = service.latest_frame()
        if latest is None:
            return
        jpeg, index, stamp = latest
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return
        # 打哈欠检测的人脸框直接画在原帧上（坐标不用换算，比画在缩放后的 pixmap 上简单）
        self._draw_faces(frame)
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        height, width, _ = rgb.shape
        image = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            max(64, self.preview.width() - 4), max(64, self.preview.height() - 4),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.frame_label.setText('帧 #{:<7d}  {}  {}×{}'.format(index, stamp, width, height))

    def _draw_faces(self, frame) -> None:
        """把推理线程给的人脸框叠到画面上：张嘴标红、闭合标绿。"""
        import cv2

        state = self.window_ref.yawn_state
        if state is None:
            return
        snapshot = state.snapshot()
        if not snapshot.faces:
            return
        threshold = snapshot.threshold or 0.5
        for face in snapshot.faces:
            x1, y1, x2, y2, prob = face
            color = (0, 0, 235) if prob >= threshold else (0, 200, 80)   # BGR
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            # 概率是纯数字，用 cv2 画不会遇到中文乱码；状态文案由指标块显示
            cv2.putText(frame, '{:.2f}'.format(prob), (x1 + 4, max(14, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        self._refresh_yawn_tiles()
        service = self.window_ref.camera_service
        if service is None:
            return
        stats = service.stats()
        self.db_label.setText('库：' + stats['db_path'])
        self.tile_frames.set_value(str(stats['written']))
        self.tile_size.set_value(f'{stats["db_size"] / 1024 / 1024:.1f}')
        if stats.get('error'):
            self.status_pill.setText(stats['error'])
        elif service.running:
            self.status_pill.setText(f'采集中 {stats["frames"]} 帧')
        # 按钮状态跟实际采集状态同步（由「设备准备」启动时也能看到"停止采集"）
        running = service.running
        want_text = '停止采集' if running else '开始采集'
        want_name = 'Danger' if running else 'Primary'
        if self.start_button.text() != want_text:
            self.start_button.setText(want_text)
            self.start_button.setObjectName(want_name)
            self.start_button.setStyle(self.start_button.style())

    def _refresh_yawn_tiles(self) -> None:
        """把推理线程的快照刷到两个指标块与提示行上。"""
        state = self.window_ref.yawn_state
        if state is None:
            return
        snapshot = state.snapshot()

        colors = {
            'no_face': theme.PALETTE['text_dim'],
            'normal': theme.PALETTE['green'],
            'open': theme.PALETTE['amber'],
            'yawning': theme.PALETTE['red'],
        }
        fatigue_colors = {
            'normal': theme.PALETTE['green'],
            'mild': theme.PALETTE['amber'],
            'severe': theme.PALETTE['red'],
        }

        if snapshot.error:
            self.tile_fatigue.set_value('异常', accent=theme.PALETTE['red'])
            self.yawn_hint.setText(snapshot.error)
            return

        if snapshot.loading:
            self.tile_fatigue.set_value('加载中', accent=theme.PALETTE['text_dim'])
            self.yawn_hint.setText('模型加载中…')
            return

        if not snapshot.running:
            self.tile_fatigue.set_value('—', accent=theme.PALETTE['text_dim'])
            if self.yawn_check.isChecked():
                self.yawn_hint.setText('未运行（开始采集后生效）')
            return

        self.tile_fatigue.set_value(
            '{}/{}'.format(snapshot.state_text, snapshot.fatigue_text),
            accent=colors.get(snapshot.state, theme.PALETTE['text_dim']))
        self.tile_yawn.set_value(str(snapshot.yawn_count),
                                 accent=fatigue_colors.get(snapshot.fatigue,
                                                           theme.PALETTE['red']))
        detail = '{} · 张口 {:.2f} · {:.1f}fps · {:.0f}ms'.format(
            snapshot.backend or '推理中', snapshot.prob, snapshot.fps, snapshot.latency_ms)
        if snapshot.state == 'no_face':
            detail += ' · 未检测到人脸'
        self.yawn_hint.setText(detail)


class MonitorWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, buffers: LiveBuffers, session_info: Optional[Dict[str, Any]] = None,
                 writer=None, status: Optional[DeviceStatus] = None,
                 ble_start=None, ble_stop=None, config=None):
        super().__init__()
        self.buffers = buffers
        self.session_info = session_info or {}
        self.writer = writer
        self.replay: Optional[ReplayPlayer] = None
        self.camera_service: Optional[CameraService] = None
        self.camera_factory = None                    # 测试/自检可注入假摄像头
        self.camera_config: Dict[str, Any] = {}
        self.status = status or DeviceStatus()
        self.ble_start = ble_start
        self.ble_stop = ble_stop
        # 打哈欠检测：配置 + 后台线程 + 状态快照（线程写、界面读）
        self.cfg = config
        self.yawn_state: Optional[YawnState] = None
        self.yawn_monitor: Optional[YawnMonitor] = None
        # 用户的意图（勾选框状态）。None = 还没表过态，按 config 的默认值走。
        # 放在窗口上而不是页面上，是为了让「不论从哪个入口启动摄像头」都能据此
        # 决定要不要自动拉起推理线程（设备准备页、命令行、脚本都会调 start_camera）。
        self.yawn_wanted: Optional[bool] = None
        # 回放页「进入分析」时把「哪个库、哪一段」放这里，分析页自己去取
        self.review_selection: Optional[Dict[str, Any]] = None
        self.setWindowTitle('蓝牙生理数据采集 · 实时监测')
        self.resize(*fit_window_size(1520, 940))
        self.setStyleSheet(theme.QSS)

        root = QWidget()
        root.setObjectName('AppRoot')
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 页面注册：按启动时勾选的设备决定建哪些页（没勾的设备页根本不存在）
        selected = set(self.session_info.get('devices') or ALL_DEVICES)
        # 只有这几台会真正产出数据；只勾了预留设备时，采集段允许直接跳过
        self.data_devices = [key for key in selected if key in DATA_DEVICES]
        self.page_specs: List[tuple] = []
        self.pages: List[BasePage] = []
        self.page_stage: Dict[int, int] = {}
        for key, title, section, cls_name, need in PAGE_SPECS:
            if need and need not in selected:
                continue
            page = globals()[cls_name](self)
            self.pages.append(page)
            self.page_specs.append((key, title, section, cls_name, need))
            self.page_stage[id(page)] = stage_of_key(key)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_stage_bar())

        # 三段整屏：采集段是标签页容器，②③ 各是一个页面
        self.stage_stack = QStackedWidget()
        self.stage_stack.addWidget(self._build_acquisition_stage())
        self.status_page = self._page_by_class('StatusPage')
        self.analysis_page = self._page_by_class('AnalysisPage')
        self.stage_stack.addWidget(self.status_page or QWidget())
        self.stage_stack.addWidget(self.analysis_page or QWidget())
        layout.addWidget(self.stage_stack, 1)

        layout.addWidget(self._build_wizard_bar())
        layout.addWidget(self._build_status())

        # 门控状态：stage=当前段，max_stage=已解锁到的最远段
        self.stage = 0
        self.max_stage = 0
        self.stage_done: Dict[int, bool] = {0: False, 1: False, 2: False}
        self.review_only = bool(self.session_info.get('review_only'))
        self._acq_base: Dict[str, int] = {}
        self._acq_attempted = False
        if self.review_only:                       # 回看模式：跳过采集段
            self.stage_done[0] = True
            self.max_stage = 1
        self.goto_stage(1 if self.review_only else 0, force=True)

        self._prev_counters: Dict[str, int] = {}
        self.last_rates: Dict[str, float] = {}
        self._last_tick = time.monotonic()
        self.data_timer = QTimer(self)
        self.data_timer.timeout.connect(self._on_data)
        self.data_timer.start(40)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._on_tick)
        self.tick_timer.start(1000)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()

    # ------------------------------------------------------------- 布局
    def _page_by_class(self, name: str):
        for page in self.pages:
            if type(page).__name__ == name:
                return page
        return None

    def _build_stage_bar(self) -> QWidget:
        """顶部阶段条：① 采集 ─ ② 回放与选段 ─ ③ 数据分析。

        未解锁的阶段显示成灰色锁态，点了进不去；已完成的打勾。
        """
        bar = QFrame()
        bar.setObjectName('StageBar')
        bar.setFixedHeight(theme.scaled(46))
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(6)
        self.stage_chips: List[QPushButton] = []
        for index, (name, _hint) in enumerate(STAGE_SPECS):
            chip = QPushButton('{}  {}'.format(index + 1, name))
            chip.setObjectName('StageChip')
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _checked=False, i=index: self.goto_stage(i))
            layout.addWidget(chip)
            self.stage_chips.append(chip)
            if index < len(STAGE_SPECS) - 1:
                arrow = label('→', 'StageArrow')
                layout.addWidget(arrow)
        layout.addStretch(1)
        self.stage_pill = pill('第 1 / 3 步')
        layout.addWidget(self.stage_pill)
        return bar

    def _build_acquisition_stage(self) -> QWidget:
        """采集段：一个标签页容器，设备准备 + 各设备页 + 实时结果。"""
        self.acq_tabs = QTabWidget()
        self.acq_tabs.setObjectName('AcqTabs')
        self.acq_tabs.setDocumentMode(True)
        for page in self.pages:
            if self.page_stage.get(id(page)) != 0:
                continue
            self.acq_tabs.addTab(page, page.title)
        if self.acq_tabs.count() == 0:
            # 一个设备都没勾：给个明确的空态，而不是白屏
            empty = QWidget()
            box = QVBoxLayout(empty)
            box.addStretch(1)
            tip = label('启动时没有选择任何测量工具', 'PageDesc')
            tip.setAlignment(Qt.AlignCenter)
            box.addWidget(tip)
            box.addStretch(1)
            self.acq_tabs.addTab(empty, '采集')
        return self.acq_tabs

    def _build_wizard_bar(self) -> QWidget:
        """底部向导条：上一步 / 当前阶段提示 / 下一步。"""
        bar = QFrame()
        bar.setObjectName('WizardBar')
        bar.setFixedHeight(theme.scaled(54))
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)
        self.prev_button = QPushButton('上一步')
        self.prev_button.setMinimumWidth(96)
        self.prev_button.clicked.connect(self.goto_prev)
        layout.addWidget(self.prev_button)
        self.stage_hint = label('', 'CardHint')
        layout.addWidget(self.stage_hint, 1)
        self.next_button = QPushButton('下一步')
        self.next_button.setObjectName('Primary')
        self.next_button.setMinimumWidth(150)
        self.next_button.clicked.connect(self.goto_next)
        layout.addWidget(self.next_button)
        return bar

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName('HeaderBar')
        header.setFixedHeight(theme.scaled(56))
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(10)
        box = QVBoxLayout()
        box.setSpacing(0)
        self.header_title = label('心电', 'HeaderTitle')
        self.header_sub = label('', 'HeaderSub')
        box.addWidget(self.header_title)
        box.addWidget(self.header_sub)
        layout.addLayout(box)
        layout.addStretch(1)
        self.header_start_button = QPushButton('开始采集')
        self.header_start_button.setObjectName('Primary')
        self.header_start_button.setMinimumWidth(96)
        self.header_start_button.clicked.connect(self.connect_devices)
        layout.addWidget(self.header_start_button)
        self.stop_button = QPushButton('结束采集')
        self.stop_button.setObjectName('Danger')
        self.stop_button.setMinimumWidth(96)
        self.stop_button.clicked.connect(self.stop_acquisition)
        layout.addWidget(self.stop_button)
        self.source_pill = pill(self.session_info.get('source', '实时采集'))
        layout.addWidget(self.source_pill)
        self.clock_pill = pill('--:--:--')
        layout.addWidget(self.clock_pill)
        return header

    def _build_status(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName('StatusBar')
        bar.setFixedHeight(theme.scaled(26))
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)
        self.status_dot = dot(theme.PALETTE['text_faint'])
        layout.addWidget(self.status_dot)
        self.status_text = label('就绪', 'BrandSub')
        layout.addWidget(self.status_text)
        layout.addStretch(1)
        info = self.session_info
        parts = [info.get('db_path', '')]
        if info.get('subject'):
            parts.append('被试 ' + info['subject'])
        if info.get('server'):
            parts.append('总线 ' + info['server'])
        self.status_right = label('  |  '.join(p for p in parts if p), 'BrandSub')
        layout.addWidget(self.status_right)
        return bar

    # ------------------------------------------------------------- 阶段门控
    def page_index(self, name: str) -> int:
        """按类名找页面下标；找不到返回 -1。"""
        for index, page in enumerate(self.pages):
            if type(page).__name__ == name:
                return index
        return -1

    def can_enter(self, stage: int) -> bool:
        """能不能进这一段：已解锁的都能进；回看模式下采集段永远进不去。"""
        if stage < 0 or stage >= len(STAGE_SPECS):
            return False
        if self.review_only and stage == 0:
            return False
        return stage <= self.max_stage or stage == self.stage

    def goto_stage(self, stage: int, force: bool = False) -> bool:
        """切换阶段的唯一入口。未解锁时返回 False，不改任何界面。"""
        if not force and not self.can_enter(stage):
            self._flash_stage_denied(stage)
            return False
        self.stage = stage
        self.stage_stack.setCurrentIndex(stage)
        if stage == 1 and self.status_page is not None:
            # 新录的会话库要刷新一次下拉，否则刚采完的数据看不见。
            # 包一层 try：某个库损坏/被占用时，不能把"切阶段"这件事一起带崩。
            refresh = getattr(self.status_page, '_refresh_sessions', None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as exc:              # noqa: BLE001
                    print('[阶段] 刷新会话列表失败：{}'.format(exc), flush=True)
        page = self.stage_stack.widget(stage)
        hook = getattr(page, 'on_show', None)
        if callable(hook):
            hook()
        self._sync_stage_ui()
        return True

    def goto_next(self) -> None:
        """「下一步」：当前段的事没做完就原地不动并提示原因。"""
        stage = self.stage
        if stage == 0:
            self.stop_acquisition()          # 内部会判数据、解锁并前进
            return
        if stage == 1:
            if self.status_page is not None and hasattr(self.status_page, 'confirm_selection'):
                self.status_page.confirm_selection()
            return
        if stage == 2:
            self._finish_flow()
            return

    def goto_prev(self) -> None:
        if self.stage > 0 and self.can_enter(self.stage - 1):
            self.goto_stage(self.stage - 1)

    def mark_stage_done(self, stage: int) -> None:
        """标记某段完成，并解锁下一段。"""
        self.stage_done[stage] = True
        self.max_stage = max(self.max_stage, min(stage + 1, len(STAGE_SPECS) - 1))
        self._sync_stage_ui()

    def invalidate_from(self, stage: int) -> None:
        """重新开始采集时把后面几段的完成状态收回。"""
        for index in range(stage, len(STAGE_SPECS)):
            self.stage_done[index] = False
        self.max_stage = min(self.max_stage, stage)
        self._sync_stage_ui()

    def _flash_stage_denied(self, stage: int) -> None:
        hints = {
            0: '回看模式下不进入采集',
            1: '先完成采集（结束采集并有数据）才能进入回放',
            2: '先在回放页选定一段区间并点「下一步」',
        }
        self.stage_hint.setText(hints.get(stage, '这一步还没解锁'))
        self.stage_hint.setStyleSheet('color: #C00000;')

    def _sync_stage_ui(self) -> None:
        """按当前阶段刷新：阶段条勾选/锁态、向导条按钮、header 按钮显隐。"""
        for index, chip in enumerate(getattr(self, 'stage_chips', [])):
            done = self.stage_done.get(index, False)
            locked = not self.can_enter(index)
            label_text = '{}  {}'.format(index + 1, STAGE_SPECS[index][0])
            if done:
                label_text = '✓ ' + label_text
            chip.setText(label_text)
            chip.setEnabled(not locked)
            chip.setChecked(index == self.stage)

        name, hint = STAGE_SPECS[self.stage]
        self.header_title.setText(name)
        self.header_sub.setText(hint)
        self.header_sub.setVisible(bool(hint))
        if hasattr(self, 'stage_pill'):
            self.stage_pill.setText('第 {} / {} 步'.format(self.stage + 1, len(STAGE_SPECS)))

        in_acq = self.stage == 0
        if hasattr(self, 'header_start_button'):
            self.header_start_button.setVisible(in_acq)
            self.stop_button.setVisible(in_acq)

        self.prev_button.setEnabled(self.stage > 0 and self.can_enter(self.stage - 1))
        self.stage_hint.setStyleSheet('')
        if self.stage == 0:
            if self.data_devices:
                self.stage_hint.setText('连好设备后点「开始采集」；采完点「结束采集并继续」')
                self.next_button.setText('结束采集并继续')
            else:
                self.stage_hint.setText('所选设备都是预留设备（无数据源），可直接跳过采集')
                self.next_button.setText('跳过采集并继续')
            self.next_button.setEnabled(True)
        elif self.stage == 1:
            self.stage_hint.setText('选会话 → 拖出区间（或点「自动选区」）→ 下一步')
            self.next_button.setText('下一步：数据分析')
            self.next_button.setEnabled(True)
        else:
            self.stage_hint.setText('勾选模型后点「特征提取」；跑完即可完成')
            self.next_button.setText('完成')
            self.next_button.setEnabled(self.stage_done.get(2, False))

    def switch_page(self, index: int) -> None:
        """兼容层：旧的「按扁平下标切页」调用仍可用（截图工具、回看分支在用）。

        采集段的页面会顺带切到对应标签，避免"切了阶段但标签没变"。
        """
        if index < 0 or index >= len(self.pages):
            return
        page = self.pages[index]
        stage = self.page_stage.get(id(page), 0)
        self.goto_stage(stage, force=True)
        if stage == 0 and hasattr(self, 'acq_tabs'):
            self.acq_tabs.setCurrentWidget(page)

    def start_replay(self, path: str, speed: float = 1.0,
                     include: Optional[List[str]] = None) -> int:
        """载入并回放。`include` 指定只回放哪几路数据（见 ui/replay.py 的 REPLAY_SOURCES）。"""
        self.stop_replay()
        packets = load_packets(path, include=include)
        self.replay = ReplayPlayer(packets, self.buffers, speed=speed)
        self.replay.start()
        page = getattr(self, 'status_page', None)
        if page is not None and hasattr(page, 'sync_session'):
            page.sync_session(path)
        self.session_info['source'] = f'回放 {os.path.basename(path)}'
        self.source_pill.setText(self.session_info['source'])
        self.status_right.setText(path)
        return len(packets)

    def stop_replay(self) -> None:
        if self.replay is not None:
            self.replay.stop()
            self.replay = None

    # ------------------------------------------------------------- 摄像头
    def default_camera_config(self) -> Dict[str, Any]:
        return {'index': 0, 'width': 640, 'height': 480, 'fps': 30.0, 'quality': 80,
                'backend': 'dshow', 'camera': 'usb0'}

    def start_camera(self, config: Optional[Dict[str, Any]] = None) -> str:
        """启动 USB 摄像头采集（帧按目标帧率入库）。"""
        if self.camera_service is not None and self.camera_service.running:
            return self.camera_service.db_path
        self.camera_config = dict(config or self.default_camera_config())
        # 帧和 BLE 传感器数据写进同一个会话库（camera_frames 表），不再另建 camera_*.db
        session_db = self.session_info.get('db_path') or None
        self.camera_service = CameraService(self.camera_config, db_path=session_db,
                                            capture_factory=self.camera_factory)
        db_path = self.camera_service.start()
        # 摄像头一起来就按需拉起打哈欠检测（默认开启）。
        # 这样不管是从摄像头页点「开始采集」、还是设备准备页/命令行启动，行为一致；
        # 用户主动取消过勾选（yawn_wanted=False）就不会被重新拉起来。
        wanted = self.yawn_wanted
        if wanted is None:
            wanted = bool(getattr(self.cfg, 'vision_enabled', True))
        if wanted:
            self.start_yawn_monitor()
        return db_path

    def stop_camera(self) -> None:
        if self.camera_service is not None:
            # 先停推理（它在读 camera_service.latest_frame），再停采集
            self.stop_yawn_monitor()
            self.camera_service.stop()
            self.camera_service = None

    # ------------------------------------------------------------- 打哈欠检测
    def _write_yawn_event(self, event: Dict[str, Any]) -> None:
        """把一次哈欠写进当前会话库，供事后分析用。

        走 SensorWriter 队列：推理线程只管判定，数据库写入由单线程串行执行，
        不会因为 SQLite 提交把推理卡住，也不会和其它采集写入抢锁。
        """
        storage = getattr(getattr(self.writer, 'recorder', None), 'storage', None)
        if storage is None:
            return
        payload = json.dumps(event, ensure_ascii=False)
        stamp = datetime.fromtimestamp(float(event['time'])).strftime(
            '%Y-%m-%d %H:%M:%S.%f')
        try:
            self.writer.submit(lambda: storage.insert('yawn_events', payload, stamp))
        except Exception:                             # noqa: BLE001 - 写不进去也不能影响检测
            pass

    def start_yawn_monitor(self) -> bool:
        """启动打哈欠推理线程。摄像头没开、或配置没启用时返回 False。"""
        if self.yawn_monitor is not None and self.yawn_monitor.is_alive():
            return True
        if self.camera_service is None:
            return False

        cfg = self.cfg
        if cfg is None:
            try:
                from core.config import Config
                cfg = Config.load()
            except Exception:                             # noqa: BLE001
                cfg = None

        self.yawn_state = self.yawn_state or YawnState()
        self.yawn_state.reset()
        monitor = YawnMonitor(
            self.camera_service,
            self.yawn_state,
            interval=getattr(cfg, 'vision_interval', 0.15),
            threshold=getattr(cfg, 'vision_threshold', 0.5),
            yawn_min_sec=getattr(cfg, 'vision_yawn_min_sec', 1.5),
            cooldown_sec=getattr(cfg, 'vision_cooldown_sec', 5.0),
            dip_grace_sec=getattr(cfg, 'vision_dip_grace', 0.3),
            face_backend=getattr(cfg, 'vision_backend', 'auto'),
            face_conf=getattr(cfg, 'vision_face_conf', 0.35),
            on_event=self._write_yawn_event,
        )
        self.yawn_monitor = monitor
        monitor.start()
        return True

    def stop_yawn_monitor(self) -> None:
        monitor, self.yawn_monitor = self.yawn_monitor, None
        if monitor is not None:
            monitor.stop()
        if self.yawn_state is not None:
            self.yawn_state.update(running=False, loading=False, faces=[], fps=0.0,
                                   state='no_face', state_text='未启动')

    def set_yawn_enabled(self, enabled: bool) -> bool:
        """界面勾选框的入口。返回是否真的处于启用状态。"""
        if enabled:
            return self.start_yawn_monitor()
        self.stop_yawn_monitor()
        return False

    # ------------------------------------------------------------- 设备准备
    def connect_devices(self) -> None:
        """一键连接：启动 BLE 扫描/连接 + 打开摄像头。"""
        self.begin_acquisition()
        self.status.set('camera', 'connecting', '', '正在打开摄像头…')
        if self.ble_start is not None:
            self.status.set('ecg', 'scanning', '', '正在扫描蓝牙…')
            self.status.set('bracelet', 'scanning', '', '正在扫描蓝牙…')
            try:
                self.ble_start()
            except Exception as exc:                  # noqa: BLE001
                self.status.set('ecg', 'error', '', str(exc))
                self.status.set('bracelet', 'error', '', str(exc))
        if self.camera_service is None or not self.camera_service.running:
            try:
                self.start_camera(self.camera_config or self.default_camera_config())
            except Exception as exc:                  # noqa: BLE001
                self.status.set('camera', 'error', '', str(exc))

    # ------------------------------------------------------------- 采集阶段
    def begin_acquisition(self) -> None:
        """开始一次采集：记录计数器基线，用于结束时判断"这次到底采到没有"。"""
        counters = self.buffers.snapshot()['counters']
        self._acq_base = dict(counters)
        self._acq_attempted = True
        self.invalidate_from(1)          # 新一轮采集，后面几段的完成状态作废
        self.session_info['acq_started_at'] = time.time()

    def acquisition_has_data(self) -> bool:
        """本次采集是否真的产生了数据。

        必须用"差值"：LiveBuffers.reset() 不清计数器，它们是进程级累计值，
        直接看绝对值会把上一次采集的存量当成这次采到的。
        """
        counters = self.buffers.snapshot()['counters']
        base = self._acq_base or {}
        for key in ('ecg_packets', 'bracelet_packets', 'ecg_samples', 'ppg_packets'):
            if counters.get(key, 0) - base.get(key, 0) > 0:
                return True
        if self.camera_service is not None:
            try:
                if self.camera_service.stats().get('written', 0) > 0:
                    return True
            except Exception:                         # noqa: BLE001
                pass
        return False

    def acquisition_summary(self) -> str:
        """这一轮到底采到了什么，用于卡住时给用户看具体数字。"""
        counters = self.buffers.snapshot()['counters']
        base = self._acq_base or {}
        parts = ['心电 {} 包'.format(counters.get('ecg_packets', 0) - base.get('ecg_packets', 0)),
                 '手环 {} 包'.format(counters.get('bracelet_packets', 0)
                                    - base.get('bracelet_packets', 0))]
        if self.camera_service is not None:
            try:
                parts.append('摄像头 {} 帧'.format(
                    self.camera_service.stats().get('written', 0)))
            except Exception:                         # noqa: BLE001
                parts.append('摄像头 统计不可用')
        else:
            parts.append('摄像头 未启动')
        return '，'.join(parts)

    def stop_acquisition(self) -> None:
        """「结束采集并继续」：停设备 → 判有没有数据 → 有才解锁回放段。"""
        if not self.data_devices:
            # 只勾了预留设备（肌电/眼动）：本来就没有数据源，不能把人卡死在这一步
            self.disconnect_devices()
            self.mark_stage_done(0)
            self.goto_stage(1)
            return
        # 必须在断开之前取：stop_camera() 会把 camera_service 置空，
        # 之后就读不到本次写入了多少帧，"有没有数据"会永远判成 False。
        has_data = self.acquisition_has_data()
        detail = self.acquisition_summary()
        device_states = {key: item.get('state')
                         for key, item in self.status.snapshot().items()}
        was_running = bool(
            (self.camera_service is not None and self.camera_service.running)
            or any(state in ('scanning', 'connecting', 'connected', 'lost')
                   for state in device_states.values()))
        self.disconnect_devices()

        if has_data:
            self.mark_stage_done(0)
            self.goto_stage(1)
            return

        # 卡住时用弹窗说清楚"卡在哪、看到了什么"，别再靠底部那行小灰字
        if not was_running and not getattr(self, '_acq_attempted', False):
            reason = ('你还没点过「开始采集」，设备一次都没启动。\n\n'
                      '先点右上角的「开始采集」，等设备连上、录一段，再点这里。')
        else:
            reason = ('设备启动过，但这次一包数据都没解出来：\n'
                      '{}。\n\n'
                      '常见原因：手环/心电贴没连上、摄像头没真正打开、'
                      '或设备连上了但没推数据。\n'
                      '可以先到「设备准备」标签页看一眼各设备的连接状态。'
                      .format(detail))
        self.stage_hint.setText('采集未完成，看弹窗说明')
        self.stage_hint.setStyleSheet('color: #C00000;')
        QMessageBox.warning(self, '还不能进入下一步', reason)

    def _finish_flow(self) -> None:
        QMessageBox.information(self, '流程完成',
                                '采集 → 回放选段 → 数据分析 已全部完成。\n'
                                '结果可用「导出 CSV」保存。')

    def enter_review_mode(self) -> None:
        """启动时选了「直接查看历史」：跳过采集段，直接进回放。"""
        self.review_only = True
        self.stage_done[0] = True
        self.max_stage = max(self.max_stage, 1)
        self.goto_stage(1, force=True)

    def disconnect_devices(self) -> None:
        """断开所有设备并复位状态。"""
        self.stop_camera()
        if self.ble_stop is not None:
            try:
                self.ble_stop()
            except Exception:                         # noqa: BLE001
                pass
        self.status.reset()

    def _on_data(self) -> None:
        snapshot = self.buffers.snapshot()
        snapshot['rates'] = self.last_rates
        for page in self.pages:
            page.on_data(snapshot)

    def _on_tick(self) -> None:
        now = time.monotonic()
        snapshot = self.buffers.snapshot()
        counters = snapshot['counters']
        dt = max(1e-3, now - self._last_tick)
        rates = {}
        if self._prev_counters and dt > 0.05:
            rates = {
                'ecg_samples': (counters['ecg_samples']
                                - self._prev_counters.get('ecg_samples', 0)) / dt,
                'ecg_packets': (counters['ecg_packets']
                                - self._prev_counters.get('ecg_packets', 0)) / dt,
                'ppg_points': (counters['ppg_samples']
                               - self._prev_counters.get('ppg_samples', 0)) / dt,
                'gsr': (counters.get('gsr_samples', 0)
                        - self._prev_counters.get('gsr_samples', 0)) / dt,
            }
        self._prev_counters = dict(counters)
        self._last_tick = now
        snapshot['rates'] = rates
        self.last_rates = rates
        for page in self.pages:
            page.on_tick(snapshot)

        # 摄像头状态同步到设备表（供「设备准备」页显示）
        service = self.camera_service
        if service is None:
            if self.status.get('camera').get('state') in ('connecting', 'connected', 'lost'):
                self.status.set('camera', 'idle', '', '未开始')
        else:
            stats = service.stats()
            if stats.get('connected'):
                self.status.set(
                    'camera', 'connected',
                    'USB index {}'.format(self.camera_config.get('index', 0)),
                    '{}  {:.0f} fps'.format(stats.get('resolution', ''),
                                            stats.get('actual_fps', 0)))
            elif stats.get('error'):
                self.status.set('camera', 'error', '', stats['error'])
            else:
                self.status.set('camera', 'connecting', '', '正在等待第一帧…')

        ages = snapshot['ages']
        active = any(age is not None and age < 3 for age in ages.values())
        self.status_dot.setStyleSheet(
            f'background-color: {theme.PALETTE["green"] if active else "#B0B0B0"};'
            f' border: 1px solid #808080;')
        if self.writer is not None:
            stats = self.writer.stats
            self.status_text.setText(
                f'写库 入队 {stats["submitted"]} / 完成 {stats["done"]} / 丢弃 {stats["dropped"]} '
                f'/ 出错 {stats["errors"]}')
        else:
            self.status_text.setText('回放模式' if self.replay else '等待数据')

        # 「结束采集」只在有设备在扫描/连接/采集时可用
        device_states = [item.get('state') for item in self.status.snapshot().values()]
        busy = any(state in ('scanning', 'connecting', 'connected', 'lost')
                   for state in device_states)
        busy = busy or (self.camera_service is not None and self.camera_service.running)
        in_acq = self.stage == 0
        self.header_start_button.setEnabled(not busy and in_acq)
        self.stop_button.setEnabled(busy and in_acq)

    def _update_clock(self) -> None:
        self.clock_pill.setText(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def closeEvent(self, event):                      # noqa: N802
        self.stop_replay()
        self.stop_camera()                            # 内部会先停打哈欠推理线程
        self.stop_yawn_monitor()                      # 摄像头没开也要确保线程退出
        if self.ble_stop is not None:
            try:
                self.ble_stop()
            except Exception:                         # noqa: BLE001
                pass
        super().closeEvent(event)


def fit_window_size(width: int, height: int, width_ratio: float = 0.96,
                    height_ratio: float = 0.92) -> Tuple[int, int]:
    """按可用屏幕收一下初始窗口尺寸。

    开了 High-DPI 缩放后，"逻辑分辨率"会变小（2560×1600 @150% → 1707×1067），
    写死的 1520×940 在更小的屏上会超出边界、右边一截够不着。
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return width, height
    avail = screen.availableGeometry()
    return (min(width, int(avail.width() * width_ratio)),
            min(height, int(avail.height() * height_ratio)))


def create_app(argv=None) -> QApplication:
    """建 QApplication。

    注意：High-DPI 属性必须在 QApplication 实例化**之前**设置，否则不生效。
    本机是 2560×1600 + 系统缩放 150%，但 Qt 默认 devicePixelRatio=1.0，
    于是 QSS 里的 12px 就按 12 个物理像素画 —— 这是"界面字太小"的真正原因，
    不是字号数值本身。打开后 Qt 会按屏幕缩放比换算所有逻辑像素，
    字和控件一起变大，而且不再发虚。
    """
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        # Qt5 默认把系统给的 1.5 倍**四舍五入成 2.0**，逻辑分辨率只剩 1280×800，
        # 主窗口 1520×940 会直接超出屏幕。PassThrough 保留精确倍率。
        try:
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        except AttributeError:                        # Qt < 5.14
            pass
    app = QApplication.instance() or QApplication(argv or [])
    app.setStyle('Fusion')
    theme.load_fonts()
    app_font = QFont(theme.FONT_UI)
    app_font.setPointSizeF(theme.scaled_pt(10))
    app.setFont(app_font)
    return app
