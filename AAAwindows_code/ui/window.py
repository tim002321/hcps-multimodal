# -*- coding: utf-8 -*-
"""主窗口：心电贴 / 绿光手环 / 状态 三个页面（浅色工业风）。

版式参照现场数据监控界面：
    左侧蓝色导航 | 中间 2 列曲线网格（黑底、黄色游标）| 右侧「待选/已选参数」+ 添加/删除
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QImage, QPixmap
from PyQt5.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
                             QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
                             QPushButton, QProgressBar, QScrollArea, QStackedWidget,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.device_status import STATE_TEXT, DeviceStatus
from devices.camera_receiver import CameraService
from ui import theme
from ui.base import BasePage, CHART_COLORS, ChannelGridPage, ac_couple
from ui.buffers import (ECG_ACCEL_RATE, ECG_RATE, RESP_RATE, LiveBuffers, ecg_signal_quality,
                        estimate_heart_rate, estimate_resp_rate, resp_has_signal)
from ui.pages_extra import AnalysisPage, EmgPage, EyePage, RecognizePage
from ui.replay import ReplayPlayer, list_sessions, load_packets
from ui.widgets import (Card, ChartPanel, NavItem, PageBackdrop, ParamListPanel, SectionBar,
                        StatTile, dot, label, mono, pill)

# 两级导航：一级是工作阶段，二级是具体页面。
# 列表顺序 == self.pages 的顺序，switch_page 用扁平索引。
NAV_SECTIONS = [
    ('采集', [('00', '设备准备'), ('01', '心电'), ('02', '手环'), ('03', '摄像头'),
              ('04', '肌电'), ('05', '眼动')]),
    ('数据查看', [('06', '会话与回放')]),
    ('状态识别', [('07', '实时结果')]),
    ('数据分析', [('08', '区间分析')]),
]

class PrepPage(BasePage):
    """设备准备：一键连接心电贴 / 手环 / 摄像头，显示连接状态。"""

    title = '设备准备'
    subtitle = '连接心电贴、绿光手环与 USB 摄像头，全部就绪后开始采集'
    DEVICES = [('ecg', '心电贴', 'BLE 低功耗蓝牙'),
               ('bracelet', '绿光手环', 'BLE 低功耗蓝牙'),
               ('camera', 'USB 摄像头', '本地 UVC 设备')]

    def build(self) -> None:
        head = QHBoxLayout()
        head.addWidget(label('设备连接检查', 'PageTitle'))
        head.addStretch(1)
        self.overall_pill = pill('未连接', theme.PALETTE['text_dim'], '#F0F0F0', '#B0B0B0')
        head.addWidget(self.overall_pill)
        self.content.addLayout(head)

        self.banner = QLabel('尚未连接设备，点击「开始连接」扫描蓝牙并打开摄像头')
        self.banner.setStyleSheet('background-color: #F7F7F7; border: 1px solid #B0B0B0;'
                                  ' padding: 8px 12px; color: #555555; font-weight: bold;')
        self.content.addWidget(self.banner)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.device_widgets: Dict[str, Dict[str, Any]] = {}
        for key, title, hint in self.DEVICES:
            card = Card(title, hint)
            row = QHBoxLayout()
            marker = dot(theme.PALETTE['text_faint'])
            row.addWidget(marker)
            state = label('未开始', 'PageDesc')
            state.setStyleSheet('font-size: 14px; font-weight: bold; color: #555555;')
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
                'font-size: 14px; font-weight: bold; color: {};'.format(color))
            detail = item.get('detail') or '—'
            widgets['detail'].setText(detail)

        ecg = states.get('ecg', {}).get('state', 'idle')
        bracelet = states.get('bracelet', {}).get('state', 'idle')
        camera = states.get('camera', {}).get('state', 'idle')
        ready = all(state == 'connected' for state in (ecg, bracelet, camera))
        if ready:
            self.overall_pill.setText('设备已就绪')
            self.overall_pill.setStyleSheet(
                'border-radius: 2px; padding: 3px 12px; font-size: 12px;'
                ' background-color: #E8F5E9; border: 1px solid {}; color: {};'
                .format(theme.PALETTE['green'], theme.PALETTE['green']))
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
                'border-radius: 2px; padding: 3px 12px; font-size: 12px;'
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
    subtitle = '心电贴：80 点/包的 244 字节报文 → ECG / 呼吸 / 三轴 / 体温'
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
        self.tile_hr = StatTile('心率', 'bpm', '--', theme.PALETTE['red'], '峰值法实时估计')
        self.tile_temp = StatTile('体温', '℃', '--', theme.PALETTE['red'], '来自心电包')
        self.tile_resp = StatTile('呼吸率', '次/分', '--', theme.PALETTE['red'], '15 秒窗口')
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
            f'border-radius: 2px; padding: 3px 10px; font-size: 11px;'
            f'background-color: #F7F7F7; border: 1px solid {color}; color: {color};')

        bpm = estimate_heart_rate(ecg, fs_ecg)
        self.tile_hr.set_value(f'{bpm:.0f}' if bpm else '--',
                               note='峰值法实时估计' if bpm else quality_text)
        if snapshot['temp_last'] is not None:
            self.tile_temp.set_value(f'{snapshot["temp_last"]:.2f}')
        resp = np.asarray(snapshot['resp'])
        if resp_has_signal(resp):
            resp_rate = estimate_resp_rate(resp, fs_resp)
            if resp_rate:
                self.tile_resp.set_value(f'{resp_rate:.1f}')
        else:
            self.tile_resp.set_value('--', note='无呼吸数据（该路固定为 0）')
        age = snapshot['ages'].get('ecg')
        state = '未连接' if age is None else ('接收中' if age < 3 else f'{age:.0f} 秒无数据')
        self.info_label.setText(
            f'状态 {state} · 心电包 {counters["ecg_packets"]} · 采样点 '
            f'{counters["ecg_samples"]}')


class BraceletPage(ChannelGridPage):
    title = '手环'
    subtitle = '绿光手环：0x30 四通道 PPG + 腕部三轴，0x21 皮电（GSR）'
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
        self.tile_gsr = StatTile('GSR 均值', '', '--', theme.PALETTE['red'], '皮肤电导')
        self.tile_skin = StatTile('肤温', '℃', '--', theme.PALETTE['red'], '手环上报')
        self.tile_battery = StatTile('电量', '%', '--', theme.PALETTE['red'], '手环上报')
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
            f'border-radius: 2px; padding: 3px 10px; font-size: 11px;'
            f'background-color: #F7F7F7; border: 1px solid {color}; color: {color};')
        self.info_label.setText(
            f'状态 {state} · 手环包 {counters["bracelet_packets"]} · PPG 包 '
            f'{counters["ppg_packets"]} · GSR 包 {counters["gsr_packets"]}')


class StatusPage(BasePage):
    title = '会话与回放'
    subtitle = '选择 data/ 下的会话回放；确认后可直接删除不需要的会话库'

    def build(self) -> None:
        head = QHBoxLayout()
        head.addWidget(label('系统运行状态', 'PageTitle'))
        head.addStretch(1)
        self.content.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(8)
        left = QVBoxLayout()
        left.setSpacing(8)

        source_card = Card('数据源')
        for name in ('ecg', 'bracelet'):
            row = QHBoxLayout()
            marker = dot(theme.PALETTE['text_faint'])
            text = label('等待数据', 'PageDesc')
            row.addWidget(marker)
            row.addWidget(text, 1)
            source_card.add_layout(row)
            setattr(self, f'{name}_dot', marker)
            setattr(self, f'{name}_text', text)
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
        self.stats_table.setMinimumHeight(220)
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
        self.replay_progress = QProgressBar()
        self.replay_progress.setRange(0, 100)
        self.replay_progress.setValue(0)
        self.replay_progress.setTextVisible(False)
        self.replay_progress.setFixedHeight(14)
        replay_card.add(self.replay_progress)
        self.replay_info = mono('选择 data/ 下的会话文件，或 .jsonl')
        self.replay_info.setWordWrap(True)
        replay_card.add(self.replay_info)
        right.addWidget(replay_card)

        help_card = Card('说明')
        help_text = label(
            '· 心电贴：244 字节包，80 点/包，约 250Hz；\n'
            '· 手环：PPG 10 点/包 × 4 通道，GSR 10 点/包；\n'
            '· 没硬件时用「回放」看历史数据；\n'
            '· 曲线参数可在左/右两页用「待选/已选」增删。', 'PageDesc')
        help_text.setWordWrap(True)
        help_card.add(help_text)
        right.addWidget(help_card)
        body.addLayout(right, 1)
        self.content.addLayout(body)

        preview_row = QHBoxLayout()
        preview_row.setSpacing(8)
        self.preview_ecg = ChartPanel('回放预览 · 心电', 'ADC')
        self.preview_ecg.add_curve('ECG', '#00D0FF', 1.3)
        self.preview_ppg = ChartPanel('回放预览 · PPG 通道 1', 'ADC')
        self.preview_ppg.add_curve('PPG1', '#00E676', 1.3)
        camera_card = Card('回放预览 · 摄像头')
        self.preview_camera = QLabel('无摄像头帧')
        self.preview_camera.setAlignment(Qt.AlignCenter)
        self.preview_camera.setMinimumHeight(220)
        self.preview_camera.setStyleSheet('background-color: #101010; color: #C8C8C8;'
                                          ' border: 1px solid #808080;')
        camera_card.add(self.preview_camera, 1)
        preview_row.addWidget(self.preview_ecg, 3)
        preview_row.addWidget(self.preview_ppg, 2)
        preview_row.addWidget(camera_card, 3)
        self.content.addLayout(preview_row, 1)
        self._refresh_sessions()

    def on_data(self, snapshot: Dict[str, Any]) -> None:
        """回放预览：把当前缓冲里的心电和 PPG1 画出来。"""
        rates = snapshot.get('rates') or {}
        fs_ecg = rates.get('ecg_samples') or ECG_RATE
        if fs_ecg < 50:
            fs_ecg = ECG_RATE
        counters = snapshot.get('counters') or {}
        self.preview_ecg.set_series('ECG', snapshot['ecg'], fs_ecg,
                                    end_time=counters.get('ecg_samples', 0) / fs_ecg)
        ppg_rate = rates.get('ppg_points') or 130.0
        ppg = snapshot['ppg']['PPG1']
        self.preview_ppg.set_series('PPG1', ac_couple(ppg, max(8, int(ppg_rate * 0.8))),
                                    ppg_rate,
                                    end_time=counters.get('ppg_samples', 0) / ppg_rate)
        self._update_camera_preview(snapshot)

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

    def _refresh_sessions(self) -> None:
        self.replay_combo.clear()
        for path, mtime, size in list_sessions('data'):
            stamp = datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')
            self.replay_combo.addItem(
                f'{os.path.basename(path)}   {stamp}   {size / 1024 / 1024:.1f} MB', path)

    def _delete_session(self) -> None:
        """删除当前选中的会话库（连同 -wal / -shm）：双确认，删前把文件名和大小写清楚。"""
        path = self.replay_combo.currentData()
        if not path or not os.path.exists(path):
            self.replay_info.setText('没有可删除的文件')
            return
        if self.window_ref.replay is not None:
            self.window_ref.stop_replay()          # 正在回放的库不能删

        size = os.path.getsize(path) / 1024 / 1024
        answer = QMessageBox.question(
            self, '确认删除',
            '将永久删除这个会话库，无法恢复：\n\n{}\n{:.1f} MB\n\n'
            '（同目录的 -wal / -shm 一并删除）'.format(os.path.basename(path), size),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            self.replay_info.setText('已取消删除')
            return
        removed = 0
        for suffix in ('', '-wal', '-shm'):
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
        count = window.start_replay(path, self.speed_spin.value())
        self.replay_button.setText('停止回放')
        self.replay_button.setObjectName('Danger')
        self.replay_button.setStyle(self.replay_button.style())
        self.replay_info.setText(
            f'回放 {os.path.basename(path)} · {count} 包 · {self.speed_spin.value():g}x')

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        counters = snapshot['counters']
        ages = snapshot['ages']
        for name, marker, text in (('ecg', self.ecg_dot, self.ecg_text),
                                   ('bracelet', self.bracelet_dot, self.bracelet_text)):
            age = ages.get(name)
            title = '心电贴' if name == 'ecg' else '手环'
            if age is None:
                marker.setStyleSheet(f'background-color: #B0B0B0; border: 1px solid #808080;')
                text.setText(f'{title}：等待数据')
            elif age < 3:
                marker.setStyleSheet(f'background-color: {theme.PALETTE["green"]};'
                                     f' border: 1px solid #808080;')
                text.setText(f'{title}：接收中（{age * 1000:.0f} ms 前）')
            else:
                marker.setStyleSheet(f'background-color: {theme.PALETTE["amber"]};'
                                     f' border: 1px solid #808080;')
                text.setText(f'{title}：{age:.0f} 秒没有数据')

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
            self.replay_progress.setValue(int(fraction * 100))
            source = self.window_ref.session_info.get('source', '回放')
            self.replay_info.setText('{} · 第 {}/{} 包 · 倍速 {:g}x'.format(
                source, position + 1, total, player.speed))
            want_text, want_style = '停止回放', 'Danger'
        else:
            if player is not None:
                self.window_ref.stop_replay()          # 自然放完
                self.replay_progress.setValue(100)
                self.replay_info.setText('回放结束')
            want_text, want_style = '开始回放', 'Primary'
        if self.replay_button.text() != want_text:
            self.replay_button.setText(want_text)
            self.replay_button.setObjectName(want_style)
            self.replay_button.setStyle(self.replay_button.style())


class CameraPage(BasePage):
    """USB 摄像头：实时预览 + 30fps 截帧入库。"""

    title = '摄像头'
    subtitle = 'USB 摄像头 → 按目标帧率截帧 → JPEG 存 SQLite（camera_frames），供后续分析'

    def build(self) -> None:
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tile_frames = StatTile('已入库帧', '帧', '0', theme.PALETTE['red'], 'camera_frames')
        self.tile_size = StatTile('库大小', 'MB', '0.0', theme.PALETTE['red'], '当前会话库')
        for tile in (self.tile_frames, self.tile_size):
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
                                   ' border: 1px solid #808080; font-size: 13px;')
        preview_card.add(self.preview, 1)
        self.frame_label = mono('—')
        preview_card.add(self.frame_label)
        body.addWidget(preview_card, 3)

        settings = Card('采集设置')
        settings.setFixedWidth(300)
        row = QHBoxLayout()
        row.addWidget(label('设备号', 'Dim'))
        self.device_spin = QDoubleSpinBox()
        self.device_spin.setDecimals(0)
        self.device_spin.setRange(0, 9)
        self.device_spin.setValue(0)
        self.device_spin.setFixedWidth(70)
        row.addWidget(self.device_spin)
        row.addStretch(1)
        settings.add_layout(row)

        row2 = QHBoxLayout()
        row2.addWidget(label('分辨率', 'Dim'))
        self.size_combo = QComboBox()
        for text, (w, h) in (('640×480', (640, 480)), ('1280×720', (1280, 720)),
                             ('1920×1080', (1920, 1080))):
            self.size_combo.addItem(text, (w, h))
        self.size_combo.setCurrentIndex(1)
        row2.addWidget(self.size_combo, 1)
        settings.add_layout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(label('目标帧率', 'Dim'))
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 60.0)
        self.fps_spin.setValue(30.0)
        self.fps_spin.setFixedWidth(80)
        row3.addWidget(self.fps_spin)
        row3.addWidget(label('fps', 'Dim'))
        row3.addStretch(1)
        settings.add_layout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(label('JPEG 质量', 'Dim'))
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

        self.db_label = mono('未开始')
        self.db_label.setWordWrap(True)
        settings.add(self.db_label)
        settings.add(label('帧以 JPEG BLOB 存进 camera_frames 表，\n'
                           '与本会话的心电/手环数据同一个库；\n'
                           '640×480 / 30fps 约 0.9 MB/秒。', 'CardHint'))
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
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        height, width, _ = rgb.shape
        image = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            max(64, self.preview.width() - 4), max(64, self.preview.height() - 4),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.frame_label.setText('帧 #{:<7d}  {}  {}×{}'.format(index, stamp, width, height))

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
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


class MonitorWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, buffers: LiveBuffers, session_info: Optional[Dict[str, Any]] = None,
                 writer=None, status: Optional[DeviceStatus] = None,
                 ble_start=None, ble_stop=None):
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
        self.setWindowTitle('蓝牙生理数据采集 · 实时监测')
        self.resize(1520, 940)
        self.setStyleSheet(theme.QSS)

        root = QWidget()
        root.setObjectName('AppRoot')
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._build_header())
        self.stack = QStackedWidget()
        self.pages = [PrepPage(self), EcgPage(self), BraceletPage(self), CameraPage(self),
                      EmgPage(self), EyePage(self), StatusPage(self), RecognizePage(self),
                      AnalysisPage(self)]
        for page in self.pages:
            self.stack.addWidget(page)
        right_layout.addWidget(self.stack, 1)
        right_layout.addWidget(self._build_status())
        layout.addWidget(right, 1)
        self.switch_page(0)

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
    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(200)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(4)
        brand = QHBoxLayout()
        brand.setContentsMargins(10, 0, 8, 0)
        brand.setSpacing(8)
        logo = theme.logo_path()
        if logo:
            badge = QLabel()
            badge.setPixmap(QPixmap(logo).scaled(36, 36, Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
            brand.addWidget(badge)
        box = QVBoxLayout()
        box.setSpacing(0)
        box.addWidget(label('Hcps · BLE', 'BrandName'))
        box.addWidget(label('生理数据采集', 'BrandSub'))
        brand.addLayout(box)
        brand.addStretch(1)
        layout.addLayout(brand)
        layout.addWidget(label('功能导航', 'NavSection'))
        self.nav_items: List[NavItem] = []
        index = 0
        for section, items in NAV_SECTIONS:
            layout.addWidget(label(section, 'NavSection'))
            for badge_text, text in items:
                item = NavItem(badge_text, text)
                item.clicked.connect(lambda _, i=index: self.switch_page(i))
                item.setProperty('NavChild', 'true')
                layout.addWidget(item)
                self.nav_items.append(item)
                index += 1
        layout.addStretch(1)
        layout.addWidget(label('ECG / PPG / GSR / EMG / EYE', 'BrandSub'))
        return sidebar

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName('HeaderBar')
        header.setFixedHeight(56)
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
        self.stop_button.clicked.connect(self.disconnect_devices)
        layout.addWidget(self.stop_button)
        self.source_pill = pill(self.session_info.get('source', '实时采集'))
        layout.addWidget(self.source_pill)
        self.clock_pill = pill('--:--:--')
        layout.addWidget(self.clock_pill)
        return header

    def _build_status(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName('StatusBar')
        bar.setFixedHeight(26)
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

    # ------------------------------------------------------------- 交互
    def switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for position, item in enumerate(self.nav_items):
            item.setChecked(position == index)
        page = self.pages[index]
        self.header_title.setText(page.title)
        self.header_sub.setText(page.subtitle)

    def start_replay(self, path: str, speed: float = 1.0) -> int:
        self.stop_replay()
        packets = load_packets(path)
        self.replay = ReplayPlayer(packets, self.buffers, speed=speed)
        self.replay.start()
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
        return self.camera_service.start()

    def stop_camera(self) -> None:
        if self.camera_service is not None:
            self.camera_service.stop()
            self.camera_service = None

    # ------------------------------------------------------------- 设备准备
    def connect_devices(self) -> None:
        """一键连接：启动 BLE 扫描/连接 + 打开摄像头。"""
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
        self.header_start_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def _update_clock(self) -> None:
        self.clock_pill.setText(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def closeEvent(self, event):                      # noqa: N802
        self.stop_replay()
        self.stop_camera()
        if self.ble_stop is not None:
            try:
                self.ble_stop()
            except Exception:                         # noqa: BLE001
                pass
        super().closeEvent(event)


def create_app(argv=None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or [])
    app.setStyle('Fusion')
    theme.load_fonts()
    app.setFont(QFont(theme.FONT_UI, 9))
    return app
