# -*- coding: utf-8 -*-
"""新增页面：预留设备页 / 状态识别 / 数据分析。

之所以单独成文件：`window.py` 只保留主窗口骨架与导航，页面按用途分散，
否则主窗口会回到 1200 行以上的「什么都往里塞」状态。
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from analysis.features import analyze_packets
from core.config import DATA_DIR, Config
from ui import theme
from ui.base import BasePage
from ui.replay import format_size, list_sessions, load_packets, load_yawn_events
from ui.widgets import Card, StatTile, label, mono


# ---------------------------------------------------------------------------
# 预留设备页
# ---------------------------------------------------------------------------
class ReservedDevicePage(BasePage):
    """尚未接入的设备页。

    界面上只显示「未接入」这一个状态，不放开发说明——那些属于接入时看的材料，
    留在下面的 CHECKLIST 和 docs/ 里，不占用户视线。
    """

    DEVICE_KEY = ''
    ENV_FLAG = ''
    CHECKLIST: List[str] = []

    def build(self) -> None:
        cfg = Config.load()
        enabled = bool(getattr(cfg, self.ENV_FLAG, False))
        tip = QLabel('该设备已启用' if enabled else '未接入该设备')
        tip.setStyleSheet('color: #777777; padding: 14px 2px;')
        self.content.addWidget(tip)
        self.content.addStretch(1)

    def config_items(self, cfg: Config) -> List[Tuple[str, Any]]:
        return []


class EmgPage(ReservedDevicePage):
    title = '表面肌电'
    subtitle = ''
    DEVICE_KEY = 'emg'
    ENV_FLAG = 'emg_enabled'
    # 接入步骤留在代码里备查，不再显示到界面上
    CHECKLIST = [
        'devices/emg_receiver.py 里把 start_emg() 接到 pylsl 的 StreamInlet（骨架已写好）',
        'core/db_storage.py 增加 data_emg 表（主工程 com/db.py 里已有同名字段可参照）',
        'core/device_status.py 的 DEVICE_TEXT 增加 emg 键（已加）',
        'config.ini 的 [devices] emg_enabled 改 true',
    ]

    def config_items(self, cfg: Config):
        return [('emg_enabled', cfg.emg_enabled), ('emg_stream', cfg.emg_stream)]


class EyePage(ReservedDevicePage):
    title = '眼动仪'
    subtitle = ''
    DEVICE_KEY = 'eye'
    ENV_FLAG = 'eye_enabled'
    CHECKLIST = [
        'devices/eye_receiver.py 里把 SmartEye 解包逻辑补全（骨架已写好）',
        'core/db_storage.py 增加 data_udp 表',
        'config.ini 的 [devices] eye_enabled 改 true',
        '摄像头面捕（瞳孔直径 / 眨眼频率）见「数据分析」页的 n/a 分支，需要人脸关键点模型',
    ]

    def config_items(self, cfg: Config):
        return [('eye_enabled', cfg.eye_enabled), ('eye_udp_port', cfg.eye_udp_port)]


# ---------------------------------------------------------------------------
# 状态识别
# ---------------------------------------------------------------------------
class RecognizePage(BasePage):
    """四个模型的结果面板。模型没接入时明确写「未接入」，不拿假数据糊弄。"""

    title = '状态识别'
    subtitle = ''

    MODELS = [
        ('load', '负荷等级', '低 / 中 / 高'),
        ('emotion', '情绪状态', '正性 / 中性 / 负性'),
        ('fatigue', '疲劳程度', '清醒 / 轻度 / 重度'),
        ('attention', '注意水平', '专注 / 分心'),
    ]

    def build(self) -> None:
        cfg = Config.load()

        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tiles: Dict[str, StatTile] = {}
        for key, name, _levels in self.MODELS:
            tile = StatTile(name, '', '--', theme.PALETTE['text_dim'])
            self.tiles[key] = tile
            tiles.addWidget(tile, 1)
        self.content.addLayout(tiles)

        body = QHBoxLayout()
        body.setSpacing(8)

        detail = Card('模型明细')
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['模型', '输出', '接入状态'])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setRowCount(len(self.MODELS))
        for row, (_key, name, levels) in enumerate(self.MODELS):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(levels))
            self.table.setItem(row, 2, QTableWidgetItem('未接入'))
        # 固定 4 行，给个贴合内容的高度，别让表格被拉伸成一整块空白
        self.table.setMinimumHeight(theme.scaled(160))
        self.table.setMaximumHeight(theme.scaled(220))
        detail.add(self.table)
        body.addWidget(detail, 3)

        bus = Card('总线上传')
        for name, value in (('地址', f'{cfg.server_host}:{cfg.server_port}'),
                            ('状态', '未启用')):
            row = QHBoxLayout()
            row.addWidget(label(name, 'Dim'), 0)
            row.addWidget(label(str(value), 'PageDesc'), 1)
            bus.add_layout(row)
        bus.body.addStretch(1)          # 与左侧等高时，内容靠上，不散开
        body.addWidget(bus, 2)
        self.content.addLayout(body)
        self.content.addStretch(1)

    def on_tick(self, snapshot: Dict[str, Any]) -> None:
        # 模型接入后在这里刷新；当前保持 -- 与「未接入」
        return


# ---------------------------------------------------------------------------
# 数据分析
# ---------------------------------------------------------------------------
class AnalysisPage(BasePage):
    """选会话 → 勾时间区间 → 提特征。

    时间区间默认取整个会话，勾上「只用选中区间」后只分析 [起, 止)。
    特征算不出来的项显示 n/a 并给出原因（电极脱落 / 数据太短 / 模型未接入）。
    """

    title = '数据分析'
    subtitle = ''

    # (界面名, key)。available=False 的只占位，选中后执行按钮禁用。
    MODELS = [
        ('HRV / 脉搏特征', 'hrv', True, '规则计算（bpm / SDNN / RMSSD / LF / HF），不需要模型文件'),
        ('疲劳 · 打哈欠', 'fatigue', True, '统计录制时实时检测写入的哈欠事件（需开启打哈欠检测）'),
        ('负荷等级', 'load', False, '未接入：缺少模型权重'),
        ('情绪状态', 'emotion', False, '未接入：缺少模型权重'),
        ('注意水平', 'attention', False, '未接入：缺少模型权重'),
    ]

    def build(self) -> None:
        self.packets: List[tuple] = []
        self.bound: Tuple[float, float] = (0.0, 0.0)
        self.last_result: Optional[Dict[str, Any]] = None

        # 分析模型：先把"选哪个"的架子搭起来。
        # 目前只有 HRV/脉搏特征是真正能算的（纯规则，不需要模型文件）；
        # 其余四个模型没有权重，选中就明确禁用执行按钮，不做假结果。
        model_row = QHBoxLayout()
        model_row.setSpacing(12)
        model_row.addWidget(label('分析模型', 'Dim'))
        self.model_boxes: Dict[str, Any] = {}
        for text, key, available, hint in self.MODELS:
            box = QCheckBox(text)
            box.setToolTip(hint)
            box.setChecked(key == 'hrv')          # 默认只勾真正能算的那个
            if not available:
                box.setStyleSheet('color: #8A8A8A;')   # 可见但灰显：表示存在、尚未接入
            box.stateChanged.connect(self._sync_model)
            self.model_boxes[key] = box
            model_row.addWidget(box)
        model_row.addStretch(1)
        self.content.addLayout(model_row)
        self.model_hint = mono('')
        self.model_hint.setWordWrap(True)
        self.content.addWidget(self.model_hint)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(label('会话', 'Dim'))
        self.session_combo = QComboBox()
        self.session_combo.setMinimumWidth(380)
        bar.addWidget(self.session_combo, 1)
        self.refresh_button = QPushButton('刷新')
        self.refresh_button.clicked.connect(self.refresh_sessions)
        bar.addWidget(self.refresh_button)
        self.load_button = QPushButton('载入')
        self.load_button.setObjectName('Primary')
        self.load_button.clicked.connect(self.load_selected)
        bar.addWidget(self.load_button)
        self.content.addLayout(bar)

        range_card = Card('分析区间')
        range_row = QHBoxLayout()
        self.use_range = QCheckBox('只用选中区间')
        self.use_range.setChecked(True)
        self.use_range.stateChanged.connect(self._sync_range_enabled)
        range_row.addWidget(self.use_range)
        self.start_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.start_edit.setDisplayFormat('MM-dd HH:mm:ss')
        self.start_edit.setCalendarPopup(True)
        self.end_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.end_edit.setDisplayFormat('MM-dd HH:mm:ss')
        self.end_edit.setCalendarPopup(True)
        self.start_edit.setEnabled(False)          # 没载入会话前不可编辑
        self.end_edit.setEnabled(False)
        range_row.addWidget(label('起', 'Dim'))
        range_row.addWidget(self.start_edit)
        range_row.addWidget(label('止', 'Dim'))
        range_row.addWidget(self.end_edit)
        self.range_hint = mono('尚未载入会话')
        range_row.addWidget(self.range_hint, 1)
        range_card.add_layout(range_row)

        action_row = QHBoxLayout()
        self.extract_button = QPushButton('特征提取')
        self.extract_button.setObjectName('Primary')
        self.extract_button.clicked.connect(self.run_analysis)
        self.export_button = QPushButton('导出 CSV')
        self.export_button.clicked.connect(self.export_csv)
        self.export_button.setEnabled(False)
        action_row.addWidget(self.extract_button)
        action_row.addWidget(self.export_button)
        action_row.addStretch(1)
        range_card.add_layout(action_row)
        self.content.addWidget(range_card)

        body = QHBoxLayout()
        body.setSpacing(8)
        result_card = Card('特征结果')
        self.result_table = QTableWidget(0, 3)
        self.result_table.setHorizontalHeaderLabels(['项目', '数值', '备注'])
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        result_card.add(self.result_table, 1)
        body.addWidget(result_card, 3)

        side = QVBoxLayout()
        side.setSpacing(8)
        self.tiles: Dict[str, StatTile] = {}
        tiles = QHBoxLayout()
        tiles.setSpacing(6)
        for key, name, unit in (('hr', '心率', 'bpm'), ('pulse', '脉率', 'bpm'),
                                ('sdnn', 'SDNN', 'ms'), ('lf_hf', 'LF/HF', '')):
            tile = StatTile(name, unit, '--', theme.PALETTE['red'], '')
            self.tiles[key] = tile
            tiles.addWidget(tile, 1)
        side.addLayout(tiles)
        side.addStretch(1)
        body.addLayout(side, 2)
        self.content.addLayout(body, 1)

        self.refresh_sessions()
        self._sync_model()

    # ------------------------------------------------------------------
    def refresh_sessions(self) -> None:
        self.session_combo.clear()
        for path, mtime, size in list_sessions(DATA_DIR):
            stamp = datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')
            self.session_combo.addItem(
                f'{os.path.basename(path)}   {stamp}   {format_size(size)}', path)
        if self.session_combo.count() == 0:
            self.session_combo.addItem('（data/ 下没有会话文件）', None)

    def _checked_models(self) -> List[Tuple[str, str, bool, str]]:
        return [spec for spec in self.MODELS if self.model_boxes[spec[1]].isChecked()]

    def _sync_model(self) -> None:
        if not hasattr(self, 'extract_button'):       # 建界面过程中信号会先到
            return
        checked = self._checked_models()
        if not checked:
            self.model_hint.setText('至少勾选一个模型')
            self.extract_button.setEnabled(False)
            return
        names = [spec[0] if spec[2] else spec[0] + '（未接入）' for spec in checked]
        self.model_hint.setText('已选：' + '、'.join(names))
        self.extract_button.setEnabled(any(spec[2] for spec in checked))

    def _sync_range_enabled(self) -> None:
        enabled = self.use_range.isChecked()
        self.start_edit.setEnabled(enabled)
        self.end_edit.setEnabled(enabled)

    def load_selected(self) -> None:
        path = self.session_combo.currentData()
        if not path:
            self.range_hint.setText('没有可载入的会话')
            return
        self.range_hint.setText('载入中…')
        self.packets = load_packets(path)
        if not self.packets:
            self.range_hint.setText('该会话没有可用数据')
            return
        first, last = self.packets[0][0], self.packets[-1][0]
        self.bound = (first, last)
        self.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(first)))
        self.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(last)))
        self._sync_range_enabled()
        self.range_hint.setText('{} 个包 · 共 {:.1f} 秒'.format(
            len(self.packets), max(0.0, last - first)))
        self.run_analysis()               # 载入即先跑一遍全区间，省一次点击

    def adopt_selection(self) -> None:
        """由回放页「进入分析」调用：带着会话路径和选区过来，直接出结果。"""
        bridge = getattr(self.window_ref, 'review_selection', None)
        if not bridge or not bridge.get('path'):
            return
        path = bridge['path']
        self.refresh_sessions()
        index = self.session_combo.findData(path)
        if index < 0:                       # 会话可能在别的目录，补一项
            self.session_combo.addItem('{}（来自回放）'.format(os.path.basename(path)), path)
            index = self.session_combo.count() - 1
        self.session_combo.setCurrentIndex(index)

        self.packets = load_packets(path)
        if not self.packets:
            self.range_hint.setText('该会话没有可用数据')
            return
        first, last = self.packets[0][0], self.packets[-1][0]
        self.bound = (first, last)
        self.use_range.setChecked(True)
        self.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(bridge['start'])))
        self.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(bridge['end'])))
        self._sync_range_enabled()
        self.range_hint.setText('来自回放选区 · 共 {:.1f} 秒'.format(
            max(0.0, bridge['end'] - bridge['start'])))
        self.run_analysis()

    def _selected_range(self) -> Tuple[Optional[float], Optional[float]]:
        if not self.use_range.isChecked() or not self.packets:
            return None, None
        start = float(self.start_edit.dateTime().toSecsSinceEpoch())
        end = float(self.end_edit.dateTime().toSecsSinceEpoch())
        if end <= start:
            return None, None
        return start, end

    # ------------------------------------------------------------------
    def run_analysis(self) -> None:
        """按勾选的模型逐个跑，结果按块拼进同一张表。"""
        checked = self._checked_models()
        if not checked:
            self.range_hint.setText('至少勾选一个分析模型')
            return
        # 只有 HRV 需要载入采样包；疲劳模型读的是事件表，不该被这个门槛挡住
        if any(spec[1] == 'hrv' for spec in checked) and not self.packets:
            self.load_selected()
            if not self.packets:
                return
        start, end = self._selected_range()

        rows: List[Tuple[str, str, str]] = []
        for text, key, available, hint in checked:
            if key == 'hrv' and available:
                result = analyze_packets(self.packets, start, end)
                self.last_result = result
                rows.extend(self._hrv_rows(result))
            elif key == 'fatigue' and available:
                rows.extend(self._fatigue_rows(start, end))
            else:
                rows.append(('【{}】'.format(text), '未接入', hint))
        self._fill_rows(rows)
        self.export_button.setEnabled(bool(self.last_result))
        if rows:                       # 真的产出了结果才算"分析完成"
            mark = getattr(self.window_ref, 'mark_stage_done', None)
            if callable(mark):
                mark(2)

    def reset_results(self) -> None:
        """回退到回放段重新选段时，把上一次的结果清掉，避免看起来还有效。"""
        self.result_table.setRowCount(0)
        self.last_result = None
        self.packets = []
        self.export_button.setEnabled(False)
        self.range_hint.setText('尚未载入会话')

    def _hrv_rows(self, result: Dict[str, Any]) -> List[Tuple[str, str, str]]:
        span = result['区间'][2]
        rows: List[Tuple[str, str, str]] = [('分析时长', '{:.1f} 秒'.format(span), '选定区间')]
        for section in ('心电', '手环PPG', '眼动', '表情'):
            block = result.get(section) or {}
            rows.append(('【{}】'.format(section), block.get('状态', 'n/a'),
                         block.get('说明', '')))
            for name, value in (block.get('特征') or {}).items():
                rows.append(('    {}'.format(name),
                             'n/a' if value is None else str(value), ''))
        ecg = (result.get('心电') or {}).get('特征') or {}
        ppg = (result.get('手环PPG') or {}).get('特征') or {}
        self.tiles['hr'].set_value(str(ecg.get('bpm', '--')))
        self.tiles['pulse'].set_value(str(ppg.get('脉率', '--')))
        self.tiles['sdnn'].set_value(str(ecg.get('sdnn', '--')))
        self.tiles['lf_hf'].set_value(str(ecg.get('lf/hf', '--')))
        return rows

    def _fatigue_rows(self, start, end) -> List[Tuple[str, str, str]]:
        """疲劳模型：读会话库里的 yawn_events —— 录制时由实时检测写入的事件。"""
        path = self.session_combo.currentData()
        rows: List[Tuple[str, str, str]] = [('【疲劳 · 打哈欠】', '', '')]
        if not path:
            rows.append(('    哈欠次数', 'n/a', '没有可用的会话'))
            return rows
        events = load_yawn_events(path, start, end)
        if events is None:
            rows.append(('    哈欠次数', 'n/a',
                         '该会话没有打哈欠记录（录制时未启用检测，或是旧版本录的）'))
            return rows
        span = (end - start) if (start and end) else 0.0
        if span <= 0 and len(events) >= 2:      # 没选区间时用首末事件估跨度
            span = float(events[-1].get('time', 0)) - float(events[0].get('time', 0))
        count = len(events)
        per_min = (count * 60.0 / span) if span > 5 else None
        longest = max((item.get('duration') or 0.0 for item in events), default=0.0)
        rows.append(('    哈欠次数', str(count), '区间内累计'))
        rows.append(('    每分钟次数', 'n/a' if per_min is None else '{:.2f}'.format(per_min),
                     '区间不足 5 秒不计算'))
        rows.append(('    最长张口', '{:.1f} 秒'.format(longest), '单次张嘴持续时长'))
        level = ('重度疲劳（参考）' if count >= 4 else
                 '轻度疲劳（参考）' if count >= 2 else '正常（参考）')
        rows.append(('    疲劳分级', level, '按次数分档，阈值待标定'))
        return rows

    def _fill_rows(self, rows: List[Tuple[str, str, str]]) -> None:
        self.result_table.setRowCount(len(rows))
        for index, (name, value, note) in enumerate(rows):
            self.result_table.setItem(index, 0, QTableWidgetItem(name))
            value_item = QTableWidgetItem(value)
            if name.startswith('【'):
                value_item.setForeground(Qt.darkGray)
            self.result_table.setItem(index, 1, value_item)
            self.result_table.setItem(index, 2, QTableWidgetItem(note))

    def export_csv(self) -> None:
        if not self.last_result:
            return
        default = os.path.join(
            DATA_DIR,
            'features_{}.csv'.format(datetime.now().strftime('%Y%m%d_%H%M%S')))
        path, _filter = QFileDialog.getSaveFileName(self, '导出特征', default, 'CSV (*.csv)')
        if not path:
            return
        rows = []
        for section in ('心电', '手环PPG', '眼动', '表情'):
            block = self.last_result.get(section) or {}
            for name, value in (block.get('特征') or {}).items():
                rows.append([section, name, '' if value is None else value,
                             block.get('说明', '')])
        with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.writer(handle)
            writer.writerow(['模块', '指标', '数值', '备注'])
            writer.writerows(rows)
        self.range_hint.setText(f'已导出 {os.path.basename(path)}')
