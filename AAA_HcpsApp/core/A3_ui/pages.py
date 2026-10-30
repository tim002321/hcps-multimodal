# -*- coding: utf-8 -*-
"""A3 界面的六个页面：总览 / 采集 / 握力 / 分析 / 表情 / 设置。

页面只做显示与转发：
* 采集数据由 CollectorManager 的 sink（PacketBridge）送进来，主线程里刷新曲线；
* 分析直接调 A2（build_features / assess / analyze）；
* 握力显示用 A2 的 GripProcessor 把 18 通道原始帧加工成角度/压力/合成力。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFileDialog, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
                             QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from core.A3_ui import theme
from core.A3_ui.face_worker import FaceWorker
from core.A3_ui.widgets import (BarMeter, Card, ChartPanel, PacketBridge, StatTile, TechBackdrop,
                                dot, label, mono, pill)
from core.A2_analyze import EMOTIONS, analyze, build_features

log = logging.getLogger(__name__)


class BasePage(QWidget):
    """页面基类：科技感背景 + 可滚动内容区。"""

    title = '页面'
    subtitle = ''

    def __init__(self, window: Any):
        super().__init__()
        self.window_ref = window
        self.controller = window.controller
        self.config = window.config
        self.backdrop = TechBackdrop(self)
        self.backdrop.setGeometry(self.rect())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        inner = QWidget()
        self.content = QVBoxLayout(inner)
        self.content.setContentsMargins(22, 18, 22, 22)
        self.content.setSpacing(14)
        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll)
        self.build()

    def resizeEvent(self, event):                     # noqa: N802 - Qt 命名
        self.backdrop.setGeometry(self.rect())
        super().resizeEvent(event)

    def build(self) -> None:
        """子类搭页面。"""

    def page_header(self, title: str, subtitle: str = '') -> None:
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(label(title, 'PageTitle'))
        if subtitle:
            box.addWidget(label(subtitle, 'PageDesc'))
        self.content.addLayout(box)

    def on_packet(self, packet) -> None:
        """收到一个 DataPacket（已在主线程）。"""

    def on_tick(self) -> None:
        """每秒刷新一次。"""


class OverviewPage(BasePage):
    title = '总览'

    def build(self) -> None:
        self.page_header('多模态生理数据采集与分析',
                         '心电 · 眼动 · 表面肌电 · 握力 · 视频 · 表情　—　实时链路状态与波形')

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.ecg_tile = StatTile('心电 ECG', 'Hz', '--', theme.PALETTE['cyan'], '等待数据…')
        self.eye_tile = StatTile('眼动 Eye', 'mm', '--', theme.PALETTE['violet'], '瞳孔直径')
        self.grip_tile = StatTile('握力 Grip', 'N', '--', theme.PALETTE['amber'], '合成力')
        self.data_tile = StatTile('已入库', '条', '0', theme.PALETTE['green'], '当前会话')
        for tile in (self.ecg_tile, self.eye_tile, self.grip_tile, self.data_tile):
            tiles.addWidget(tile, 1)
        self.content.addLayout(tiles)

        charts = QHBoxLayout()
        charts.setSpacing(12)
        self.ecg_chart = ChartPanel('心电波形', 'mV', max_points=1200)
        self.ecg_chart.add_curve('ECG', theme.PALETTE['cyan'], 1.4)
        self.eye_chart = ChartPanel('瞳孔直径', 'mm', max_points=600)
        self.eye_chart.add_curve('Pupil', theme.PALETTE['violet'], 1.8)
        for chart in (self.ecg_chart, self.eye_chart):
            chart.setMaximumHeight(300)               # 别把底下的会话卡挤到视口外
        charts.addWidget(self.ecg_chart, 3)
        charts.addWidget(self.eye_chart, 2)
        self.content.addLayout(charts)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.session_card = Card('当前会话', '采集与落库')
        self.session_label = mono('未开始')
        self.session_label.setWordWrap(True)
        self.session_card.add(self.session_label)
        self.grip_chart = ChartPanel('握力合成力', 'N', max_points=600)
        self.grip_chart.add_curve('L', theme.PALETTE['cyan'], 1.8)
        self.grip_chart.add_curve('R', theme.PALETTE['amber'], 1.8)
        self.grip_chart.setMaximumHeight(220)
        bottom.addWidget(self.session_card, 2)
        bottom.addWidget(self.grip_chart, 3)
        self.content.addLayout(bottom)
        self.content.addStretch(1)
        self._ecg_points = 0

    def on_packet(self, packet) -> None:
        if packet.device == 'ecg':
            samples = packet.payload.get('ECG', [])
            for value in samples:
                self.ecg_chart.append('ECG', float(value))
            self._ecg_points += len(samples)
            self.ecg_tile.set_value(str(len(samples) * 3), note=f'已收 {self._ecg_points} 个采样点')
        elif packet.device == 'eye':
            pupil = packet.payload.get('PupilDiameter')
            if pupil is not None:
                self.eye_chart.append('Pupil', float(pupil))
                self.eye_tile.set_value(f'{float(pupil):.2f}', note='瞳孔直径 mm')
        elif packet.device == 'grip':
            self._update_grip(packet)

    def _update_grip(self, packet) -> None:
        frame = self.window_ref.grip_processor.process(packet.payload.get('raw', []),
                                                       packet.payload.get('side', 'L'))
        side = 'L' if frame['side'] == 'L' else 'R'
        self.grip_chart.append(side, frame['synthesis_force'])
        self.grip_tile.set_value(f'{frame["synthesis_force"]:.1f}', note=f'{side} 手合成力')

    def on_tick(self) -> None:
        controller = self.controller
        self.data_tile.set_value(str(controller.written), note='当前会话已入库条数')
        if controller.started_at is not None:
            elapsed = int((datetime.now() - controller.started_at).total_seconds())
            names = '、'.join(controller.status()) or '（无）'
            self.session_label.setText(
                f'库文件：{controller.db_path}\n'
                f'开始时：{controller.started_at.strftime("%Y-%m-%d %H:%M:%S")}\n'
                f'已运行：{elapsed // 60:02d}:{elapsed % 60:02d}\n'
                f'采集器：{names}')


class CollectPage(BasePage):
    title = '采集'

    def build(self) -> None:
        self.page_header('数据采集',
                         '会话目录沿用实验结果/<用户名>_<任务>_<日期>/normal_result 的老规矩')

        top = QHBoxLayout()
        top.setSpacing(12)

        setup = Card('会话设置', '开始前确认用户名与任务名')
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.user_edit = QLineEdit('smoke')
        self.task_edit = QLineEdit('ui')
        form.addWidget(label('用户名', 'Dim'), 0, 0)
        form.addWidget(self.user_edit, 0, 1)
        form.addWidget(label('任务名', 'Dim'), 0, 2)
        form.addWidget(self.task_edit, 0, 3)
        setup.add_layout(form)

        buttons = QHBoxLayout()
        self.start_button = QPushButton('开始采集')
        self.start_button.setObjectName('Primary')
        self.stop_button = QPushButton('停止采集')
        self.stop_button.setObjectName('Danger')
        self.stop_button.setEnabled(False)
        self.new_button = QPushButton('新开会话目录')
        self.new_button.setObjectName('Ghost')
        for button in (self.start_button, self.stop_button, self.new_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        setup.add_layout(buttons)
        self.db_label = mono('尚未开始')
        self.db_label.setWordWrap(True)
        setup.add(self.db_label)
        top.addWidget(setup, 3)

        source = Card('数据源', '模拟源不需要任何硬件')
        self.source_label = label('—', 'PageDesc')
        self.source_label.setWordWrap(True)
        source.add(self.source_label)
        source.add(pill('模拟数据源' if self.controller.use_sim else '真实设备（default.yaml）'))
        self.source_card = source
        top.addWidget(source, 2)
        self.content.addLayout(top)

        status_card = Card('采集器状态', '每秒刷新')
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['采集器', '状态', '包数', '错误', '最后错误'])
        self.table.verticalHeader().setVisible(False)
        for column in range(4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setMinimumHeight(140)
        status_card.add(self.table)
        self.content.addWidget(status_card)

        log_card = Card('运行日志', 'logging 直接接到界面')
        self.log_view = QTextEdit()                   # 用 QTextEdit 才能显示带颜色的等级
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(200)
        self.log_view.setStyleSheet('QTextEdit { background-color: #0B1220;'
                                    ' border: 1px solid #1E2C45; border-radius: 8px;'
                                    ' font-family: Consolas; font-size: 12px; }')
        log_card.add(self.log_view)
        self.content.addWidget(log_card)
        self.content.addStretch(1)

        self.log_lines: List[str] = []
        self.start_button.clicked.connect(lambda: self._start(False))
        self.stop_button.clicked.connect(self._stop)
        self.new_button.clicked.connect(lambda: self._start(True))
        self.source_label.setText(
            '模拟源：按固定频率合成心电/眼动/握力数据，用来跑通链路、开发界面。\n'
            '真实设备：把 config/default.yaml 里对应采集器的 enabled 改成 true。')

    def append_log(self, text: str, level: int = logging.INFO) -> None:
        color = theme.PALETTE['text_dim']
        if level >= logging.ERROR:
            color = theme.PALETTE['red']
        elif level >= logging.WARNING:
            color = theme.PALETTE['amber']
        self.log_lines.append(f'<span style="color:{color}">{text}</span>')
        del self.log_lines[:-400]
        self.log_view.setHtml('<br>'.join(self.log_lines))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _start(self, fresh: bool = False) -> None:
        if self.controller.running:
            return
        try:
            path = self.controller.start(self.user_edit.text().strip() or 'smoke',
                                         self.task_edit.text().strip() or 'ui', fresh=fresh)
        except Exception as exc:                      # noqa: BLE001
            self.append_log(f'启动失败：{exc}', logging.ERROR)
            return
        self.db_label.setText(f'会话库：{path}')
        self.append_log(f'开始采集，会话库：{path}')
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _stop(self) -> None:
        if not self.controller.running:
            return
        self.controller.stop()
        self.append_log(f'采集已停止，共入库 {self.controller.written} 条')
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.table.setRowCount(0)

    def on_tick(self) -> None:
        status = self.controller.status()
        self.table.setRowCount(len(status))
        for row, (name, info) in enumerate(status.items()):
            for column, text in enumerate([name, info['state'], str(info['packets']),
                                           str(info['errors']), info['last_error']]):
                self.table.setItem(row, column, QTableWidgetItem(text))
            if info['state'] != 'running':
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    if item is not None:
                        item.setForeground(Qt.GlobalColor.darkYellow)
        running = self.controller.running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)


class GripPage(BasePage):
    title = '握力'

    def build(self) -> None:
        self.page_header('握力手套',
                         '18 通道原始帧经 A2 加工（卡尔曼 → 角度解算 → 压力拟合）后显示')

        row = QHBoxLayout()
        row.setSpacing(12)
        self.hands: Dict[str, Dict[str, Any]] = {}
        for side, accent, caption in (('L', theme.PALETTE['cyan'], '左手'),
                                      ('R', theme.PALETTE['amber'], '右手')):
            card = Card(caption, '15 路压力 + 3 个关节角')
            angles = QHBoxLayout()
            self.hands[side] = {'meters': [], 'angles': [], 'count': 0}
            for index in range(3):
                tile = StatTile(f'角度 {index + 1}', '°', '0.0', accent)
                self.hands[side]['angles'].append(tile)
                angles.addWidget(tile, 1)
            card.add_layout(angles)
            for index in range(15):
                meter = BarMeter(f'P{index + 1:02d}', 5000.0, accent)
                self.hands[side]['meters'].append(meter)
                card.add(meter)
            force = StatTile('合成力', 'N', '0.0', theme.PALETTE['green'], '老口径权重合成')
            self.hands[side]['force'] = force
            card.add(force)
            row.addWidget(card, 1)
        self.content.addLayout(row)

        self.force_chart = ChartPanel('合成力趋势', 'N', max_points=800)
        self.force_chart.add_curve('L', theme.PALETTE['cyan'], 1.8)
        self.force_chart.add_curve('R', theme.PALETTE['amber'], 1.8)
        self.content.addWidget(self.force_chart, 1)
        self.content.addStretch(1)

    def on_packet(self, packet) -> None:
        if packet.device != 'grip':
            return
        frame = self.window_ref.grip_processor.process(packet.payload.get('raw', []),
                                                      packet.payload.get('side', 'L'))
        side = 'L' if frame['side'] == 'L' else 'R'
        hand = self.hands[side]
        hand['count'] += 1
        for index, value in enumerate(frame['angle'][:3]):
            hand['angles'][index].set_value(f'{value:.1f}')
        for index, value in enumerate(frame['press'][:15]):
            hand['meters'][index].set_value(value)
        hand['force'].set_value(f'{frame["synthesis_force"]:.1f}', note=f'已收 {hand["count"]} 帧')
        self.force_chart.append(side, frame['synthesis_force'], x=hand['count'])


class AnalyzePage(BasePage):
    title = '分析'
    FEATURES = ['bpm', 'hf', 'lf/hf', 'lf', 'vlf', 'rmssd', 'sd1', 'sd2/sd1']
    TABLE_COLUMNS = ['time', 'pupil', 'blink_rate', 'bpm', 'hf', 'lf/hf', 'lf', 'vlf',
                     'sd2/sd1', 'rmssd', 'sd1']

    def build(self) -> None:
        self.page_header('离线分析', '会话库 → 心电/眼动特征 → 三模型负荷等级（A2 分析层）')

        picker = Card('数据源', '默认填当前会话；也可以挑历史会话库')
        row = QHBoxLayout()
        self.db_edit = QLineEdit('')
        self.db_edit.setPlaceholderText('选择 database.db …')
        browse = QPushButton('浏览')
        browse.setObjectName('Ghost')
        browse.clicked.connect(self._browse)
        run = QPushButton('运行分析')
        run.setObjectName('Primary')
        run.clicked.connect(self._run)
        row.addWidget(self.db_edit, 1)
        row.addWidget(browse)
        row.addWidget(run)
        picker.add_layout(row)
        self.hint = mono('—')
        picker.add(self.hint)
        self.content.addWidget(picker)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_windows = StatTile('特征窗口', '个', '0', theme.PALETTE['green'])
        self.tile_pupil = StatTile('平均瞳孔', 'mm', '--', theme.PALETTE['violet'])
        self.tile_bpm = StatTile('平均心率', 'bpm', '--', theme.PALETTE['cyan'])
        for tile in (self.tile_windows, self.tile_pupil, self.tile_bpm):
            tiles.addWidget(tile, 1)
        self.content.addLayout(tiles)

        charts = QHBoxLayout()
        charts.setSpacing(12)
        self.feature_chart = ChartPanel('特征曲线', '', max_points=2000)
        for name, color in (('bpm', theme.PALETTE['cyan']), ('hf', theme.PALETTE['violet']),
                            ('rmssd', theme.PALETTE['amber'])):
            self.feature_chart.add_curve(name, color, 1.8)
        self.level_chart = ChartPanel('负荷等级', 'class', max_points=2000)
        for name, color in (('SVM', theme.PALETTE['cyan']), ('CatBoost', theme.PALETTE['violet']),
                            ('XGBoost', theme.PALETTE['amber'])):
            self.level_chart.add_curve(name, color, 1.6)
        charts.addWidget(self.feature_chart, 3)
        charts.addWidget(self.level_chart, 2)
        self.content.addLayout(charts)

        table_card = Card('特征与预测', '最多显示 200 行')
        self.table = QTableWidget(0, len(self.TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(self.TABLE_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(200)
        table_card.add(self.table)
        self.content.addWidget(table_card)
        self.content.addStretch(1)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, '选择会话数据库', '', '数据库 (*.db *.sqlite)')
        if path:
            self.db_edit.setText(path)

    def _run(self) -> None:
        db_path = self.db_edit.text().strip()
        if not db_path or not os.path.exists(db_path):
            self.hint.setText('请先选择有效的 database.db')
            return
        self.hint.setText('计算中…')
        QApplication.processEvents()
        analyze_config = self.config.get('analyze') or {}
        try:
            features = build_features(db_path, config=analyze_config)
            results = analyze(db_path, config=analyze_config)
        except Exception as exc:                      # noqa: BLE001 - 界面要报错，不要崩
            self.hint.setText(f'分析失败：{exc}')
            return

        models = [name for name in results.columns if name != 'time']
        self.hint.setText(f'特征 {len(features)} 行 · 预测 {len(results)} 行 · 模型 {", ".join(models)}')
        self.tile_windows.set_value(str(len(features)))
        if len(features):
            self.tile_pupil.set_value(f'{features["pupil"].mean():.2f}')
            self.tile_bpm.set_value(f'{features["bpm"].mean():.1f}')

        x = list(range(len(features)))
        for name in ('bpm', 'hf', 'rmssd'):
            if name in features:
                self.feature_chart.set_series(name, x, features[name].fillna(0).tolist())
        for name in models:
            self.level_chart.set_series(name, list(range(len(results))),
                                        [float(v) for v in results[name]])

        rows = min(len(features), 200)
        self.table.setRowCount(rows)
        for row in range(rows):
            for column, name in enumerate(self.TABLE_COLUMNS):
                text = str(features.iloc[row]['timestamp']) if name == 'time' \
                    else f'{features.iloc[row][name]}'
                self.table.setItem(row, column, QTableWidgetItem(text))

    def on_tick(self) -> None:
        if not self.db_edit.text() and self.controller.db_path:
            self.db_edit.setText(self.controller.db_path)


class FacePage(BasePage):
    title = '表情'

    def build(self) -> None:
        self.page_header('表情识别', 'BlazeFace 正脸检测 + CNN3 八类表情（每个检测框 6 张增广求和）')

        row = QHBoxLayout()
        row.setSpacing(12)

        picker = Card('识别', '选一张图片，或直接用测试图')
        self.path_edit = QLineEdit('')
        self.path_edit.setPlaceholderText('图片路径 …')
        picker.add(self.path_edit)
        buttons = QHBoxLayout()
        browse = QPushButton('选择图片')
        browse.setObjectName('Ghost')
        browse.clicked.connect(self._browse)
        run = QPushButton('识别')
        run.setObjectName('Primary')
        run.clicked.connect(self._run)
        default = QPushButton('用示例测试图')
        default.setObjectName('Ghost')
        default.clicked.connect(self._use_sample)
        for button in (browse, run, default):
            buttons.addWidget(button)
        buttons.addStretch(1)
        picker.add_layout(buttons)
        self.result_tile = StatTile('识别结果', '', '--', theme.PALETTE['cyan'], '等待识别')
        picker.add(self.result_tile)
        row.addWidget(picker, 2)

        self.preview = QLabel('图片预览')
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(320)
        self.preview.setStyleSheet('background-color: #0B1220; border: 1px solid #1E2C45;'
                                   'border-radius: 10px; color: #5B6E8C;')
        row.addWidget(self.preview, 3)
        self.content.addLayout(row)

        live = Card('实时监测', '接本机摄像头，实时框脸 + 实时表情（识别仍在子进程里跑）')
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(label('摄像头编号', 'Dim'))
        self.camera_index = QSpinBox()
        self.camera_index.setRange(0, 5)
        self.camera_index.setFixedWidth(60)
        controls.addWidget(self.camera_index)
        self.camera_button = QPushButton('打开摄像头')
        self.camera_button.setObjectName('Primary')
        self.camera_button.clicked.connect(self._toggle_camera)
        controls.addWidget(self.camera_button)
        self.monitor_check = QCheckBox('实时识别')
        self.monitor_check.setChecked(True)
        controls.addWidget(self.monitor_check)
        controls.addStretch(1)
        self.live_info = mono('摄像头未打开')
        controls.addWidget(self.live_info)
        live.add_layout(controls)
        self.live_preview = QLabel('摄像头未打开')
        self.live_preview.setAlignment(Qt.AlignCenter)
        self.live_preview.setMinimumHeight(260)
        self.live_preview.setStyleSheet('background-color: #05080F; border: 1px solid #1E2C45;'
                                        'border-radius: 10px; color: #5B6E8C;')
        live.add(self.live_preview)
        self.content.addWidget(live)

        prob_card = Card('八类概率', '六张增广的 softmax 之和（满值 6.0）')
        self.bars: Dict[str, BarMeter] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        for index, emotion in enumerate(EMOTIONS):
            meter = BarMeter(emotion, 6.0, theme.PALETTE['cyan'])
            self.bars[emotion] = meter
            grid.addWidget(meter, index // 2, index % 2)
        prob_card.add_layout(grid)
        self.content.addWidget(prob_card)
        self.content.addStretch(1)
        self.worker: Optional[FaceWorker] = None
        self.pending_path = ''
        self._live_busy = False
        self._live_sent_at = 0.0
        self._live_frames = 0
        self._live_fps_at = time.time()
        self._live_fps_frames = 0
        self._live_fps = 0.0
        self._live_latency = 0.0
        self._last_boxes: List[tuple] = []
        self._last_emotion = ''

    def _sample_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'models', 'test_photo2.png')

    def _use_sample(self) -> None:
        self.path_edit.setText(self._sample_path())
        self._run()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, '选择图片', '',
                                              '图片 (*.jpg *.jpeg *.png *.bmp)')
        if path:
            self.path_edit.setText(path)
            self._run()

    def _run(self) -> None:
        path = self.path_edit.text().strip()
        if not path or not os.path.exists(path):
            self.result_tile.set_value('--', note='请先选择图片')
            return
        self.pending_path = path
        self.result_tile.set_value('识别中', note='TensorFlow 在子进程里跑，第一次要等几秒')
        self._worker().request(path)

    def _worker(self) -> FaceWorker:
        """惰性创建识别子进程——不用表情功能就不会多起进程。"""
        if self.worker is None:
            self.worker = FaceWorker(
                os.path.join(self.window_ref.project_root, 'tools', 'face_cli.py'),
                self.window_ref.config_path)
            self.worker.result.connect(self._on_result)
            self.worker.failed.connect(self._on_failed)
            self.worker.ready.connect(lambda: log.info('[face] 识别子进程就绪'))
        return self.worker

    def _on_failed(self, message: str) -> None:
        self._live_busy = False
        self.result_tile.set_value('--', note=message, accent=theme.PALETTE['red'])

    def _on_result(self, payload: Dict[str, Any]) -> None:
        emotions = payload.get('emotions') or ['no_face']
        boxes = payload.get('boxes') or []
        probabilities = payload.get('probabilities') or []
        self._apply_result(emotions, boxes, probabilities)

        if payload.get('id') is not None:             # 实时监测的一帧：框留到下一帧画
            self._live_busy = False
            self._live_latency = max(0.0, time.time() - self._live_sent_at)
            self._last_boxes = [tuple(int(v) for v in box) for box in boxes]
            return

        # 单张图片：直接把框画在预览图上
        path = payload.get('path') or self.pending_path
        image = self.window_ref.face_analyzer.read_image(path)
        if image is None:
            return
        self._render_preview(image, boxes, self.preview)

    def _apply_result(self, emotions: List[str], boxes: List[Any],
                      probabilities: List[Any]) -> None:
        """更新右上角的结论与八类概率条（单图和实时帧共用）。"""
        emotion = emotions[0] if emotions else 'no_face'
        self._last_emotion = emotion
        self.result_tile.set_value(emotion.upper(), note=f'检出人脸 {len(boxes)} 张',
                                   accent=theme.PALETTE['cyan'] if emotion in EMOTIONS
                                   else theme.PALETTE['amber'])
        if probabilities:
            values = np.asarray(probabilities[0], dtype=float)
            for index, name in enumerate(EMOTIONS):
                if index < len(values):
                    self.bars[name].set_value(float(values[index]))

    def _render_preview(self, image, boxes, target: QLabel) -> None:
        """把检测框画上去并缩放显示到指定 QLabel。"""
        for (x, y, w, h) in boxes:
            image = self._draw_box(image, int(x), int(y), int(w), int(h))
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        height, width, _ = rgb.shape
        qimage = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888).copy()
        target.setPixmap(QPixmap.fromImage(qimage).scaled(
            max(64, target.width() - 16), max(64, target.height() - 16),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ------------------------------------------------------------- 实时监测
    def _toggle_camera(self) -> None:
        window = self.window_ref
        if window.camera is None:
            try:
                window.start_camera({'index': self.camera_index.value()})
            except Exception as exc:                  # noqa: BLE001 - 打不开就提示，别崩
                self.live_info.setText(f'打开摄像头失败：{exc}')
                return
            self.camera_button.setText('停止摄像头')
            self.camera_button.setObjectName('Danger')
            self.camera_button.setStyle(self.camera_button.style())
            self.live_info.setText('摄像头已打开，等待第一帧…')
        else:
            window.stop_camera()
            self.camera_button.setText('打开摄像头')
            self.camera_button.setObjectName('Primary')
            self.camera_button.setStyle(self.camera_button.style())
            self.live_info.setText('摄像头已关闭')

    def on_packet(self, packet) -> None:
        if packet.device != 'video_frame' or self.window_ref.camera is None:
            return
        jpeg = packet.raw
        if not jpeg:
            return
        image = self.window_ref.face_analyzer.decode_frame(jpeg)
        if image is None:
            return

        self._live_frames += 1
        self._live_fps_frames += 1
        now = time.time()
        if now - self._live_fps_at >= 1.0:
            self._live_fps = self._live_fps_frames / (now - self._live_fps_at)
            self._live_fps_at, self._live_fps_frames = now, 0

        self._render_preview(image.copy(), self._last_boxes, self.live_preview)
        meta = packet.meta or {}
        info = (f'帧 {self._live_frames} · {self._live_fps:.1f} fps · '
                f'识别 {self._live_latency * 1000:.0f} ms')
        if meta.get('brightness') is not None:
            info += f' · 亮度 {meta["brightness"]}'
        if meta.get('dark'):
            info += ' · 画面全黑：检查隐私挡板/是否被别的程序占用'
        self.live_info.setText(info)

        if self.monitor_check.isChecked() and not self._live_busy:
            self._live_busy = True
            self._live_sent_at = now
            self._worker().request_frame(jpeg, int(packet.payload.get('index') or self._live_frames))

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    @staticmethod
    def _draw_box(image, x, y, w, h):
        """在人脸上画一个青色框（纯 numpy 写像素，省得引 cv2 的绘制 API）。"""
        color = (238, 211, 34)                       # BGR 里的青色
        x, y, w, h = int(x), int(y), int(w), int(h)
        top, bottom = max(0, y), min(image.shape[0], y + h)
        left, right = max(0, x), min(image.shape[1], x + w)
        image[top:min(bottom, top + 2), left:right] = color
        image[max(top, bottom - 2):bottom, left:right] = color
        image[top:bottom, left:min(right, left + 2)] = color
        image[top:bottom, max(left, right - 2):right] = color
        return image


class SettingsPage(BasePage):
    title = '设置'

    def build(self) -> None:
        self.page_header('设置与资产', '配置来自 config/default.yaml，模型在 models/ 下')

        row = QHBoxLayout()
        row.setSpacing(12)
        config_card = Card('设备与参数', '只读展示，改配置请编辑 default.yaml')
        self.config_table = QTableWidget(0, 2)
        self.config_table.setHorizontalHeaderLabels(['配置项', '值'])
        self.config_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.config_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.config_table.verticalHeader().setVisible(False)
        self.config_table.setMinimumHeight(360)
        config_card.add(self.config_table)
        row.addWidget(config_card, 3)

        asset_card = Card('模型资产', 'A2 分析层用')
        self.asset_label = mono('')
        self.asset_label.setWordWrap(True)
        asset_card.add(self.asset_label)
        open_models = QPushButton('打开模型目录')
        open_models.setObjectName('Ghost')
        open_models.clicked.connect(lambda: self._open_dir(self.window_ref.models_dir))
        open_session = QPushButton('打开实验结果目录')
        open_session.setObjectName('Ghost')
        open_session.clicked.connect(lambda: self._open_dir(self.window_ref.results_dir))
        asset_card.add(open_models)
        asset_card.add(open_session)
        logo = theme.logo_path()
        if logo:
            holder = QLabel()
            holder.setPixmap(QPixmap(logo).scaled(96, 96, Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation))
            holder.setAlignment(Qt.AlignCenter)
            asset_card.add(holder)
        row.addWidget(asset_card, 2)
        self.content.addLayout(row)
        self.content.addStretch(1)
        self._fill()

    def _fill(self) -> None:
        rows: List[tuple] = []
        for name, section in (self.config.get('collectors') or {}).items():
            if not isinstance(section, dict):
                continue
            rows.append((f'collectors.{name}.enabled', str(section.get('enabled'))))
            for key in ('bind', 'port', 'peer_ip', 'host', 'device_name', 'camera'):
                if key in section:
                    rows.append((f'collectors.{name}.{key}', str(section[key])))
        analyze_config = self.config.get('analyze') or {}
        rows.append(('analyze.models_dir', str(analyze_config.get('models_dir'))))
        rows.append(('analyze.models', ', '.join(analyze_config.get('models') or [])))
        for group in ('ecg', 'eye', 'face'):
            for key, value in (analyze_config.get(group) or {}).items():
                rows.append((f'analyze.{group}.{key}', str(value)))

        self.config_table.setRowCount(len(rows))
        for index, (key, value) in enumerate(rows):
            self.config_table.setItem(index, 0, QTableWidgetItem(key))
            self.config_table.setItem(index, 1, QTableWidgetItem(value))

        models_dir = self.window_ref.models_dir
        lines = [f'目录：{models_dir}']
        if os.path.isdir(models_dir):
            for name in sorted(os.listdir(models_dir)):
                size = os.path.getsize(os.path.join(models_dir, name))
                lines.append(f'  {name:<30} {size / 1024:,.0f} KB')
        else:
            lines.append('  （目录不存在）')
        self.asset_label.setText('\n'.join(lines))

    @staticmethod
    def _open_dir(path: str) -> None:
        try:
            os.startfile(path)                        # noqa: S606 - Windows 专用
        except Exception as exc:                      # noqa: BLE001 - 打不开就算了
            log.warning('打开目录失败：%s（%s）', path, exc)
