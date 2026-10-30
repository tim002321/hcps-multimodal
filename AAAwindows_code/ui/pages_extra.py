# -*- coding: utf-8 -*-
"""新增页面：预留设备页 / 状态识别 / 数据分析。

之所以单独成文件：`window.py` 只保留主窗口骨架与导航，页面按用途分散，
否则主窗口会回到 1200 行以上的「什么都往里塞」状态。
"""
from __future__ import annotations

import csv
import os
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
from ui.replay import list_sessions, load_packets
from ui.widgets import Card, SectionBar, StatTile, label, mono, pill


# ---------------------------------------------------------------------------
# 预留设备页
# ---------------------------------------------------------------------------
class ReservedDevicePage(BasePage):
    """尚未接入的设备：把「接进来要做什么」写在页面上，避免下次又要翻文档。"""

    DEVICE_KEY = ''
    ENV_FLAG = ''
    CHECKLIST: List[str] = []

    def build(self) -> None:
        cfg = Config.load()
        enabled = bool(getattr(cfg, self.ENV_FLAG, False))

        head = QHBoxLayout()
        head.addWidget(label(self.title, 'PageTitle'))
        head.addStretch(1)
        head.addWidget(pill('已接入' if enabled else '预留', theme.PALETTE['green'] if enabled
                            else theme.PALETTE['text_dim'], '#F7F7F7',
                            theme.PALETTE['green'] if enabled else '#B0B0B0'))
        self.content.addLayout(head)
        self.content.addWidget(label(self.subtitle, 'PageDesc'))

        card = Card('接入清单')
        for index, item in enumerate(self.CHECKLIST, 1):
            line = label(f'{index}. {item}', 'PageDesc')
            line.setWordWrap(True)
            card.add(line)
        self.content.addWidget(card)

        cfg_card = Card('相关配置（config.ini）')
        for name, value in self.config_items(cfg):
            row = QHBoxLayout()
            row.addWidget(mono(name), 0)
            row.addWidget(label(str(value), 'PageDesc'), 1)
            cfg_card.add_layout(row)
        self.content.addWidget(cfg_card)
        self.content.addStretch(1)

    def config_items(self, cfg: Config) -> List[Tuple[str, Any]]:
        return []


class EmgPage(ReservedDevicePage):
    title = '表面肌电'
    subtitle = ('预留：走 LSL 流（与主工程 data_emg.py 同一路），接入后这里显示原始肌电、'
                'RMS 包络与中值频率。')
    DEVICE_KEY = 'emg'
    ENV_FLAG = 'emg_enabled'
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
    subtitle = ('预留：走 UDP + SmartEye 二进制解包（与主工程 data_udp.py 同一套协议），'
                '接入后这里显示注视点、瞳孔直径与眨眼。')
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
    subtitle = '四个模型的结果汇总；实时数据与模型结果按「总线 IP」上传'

    MODELS = [
        ('load', '负荷等级', '低 / 中 / 高', '10 维特征 → SVM / CatBoost / XGBoost'),
        ('emotion', '情绪状态', '正性 / 中性 / 负性', 'YOLO 表情 → 情绪映射'),
        ('fatigue', '疲劳程度', '清醒 / 轻度 / 重度', 'HRV + 眨眼频率 + 表情'),
        ('attention', '注意水平', '专注 / 分心', '眼动注视 + 头动'),
    ]

    def build(self) -> None:
        cfg = Config.load()
        head = QHBoxLayout()
        head.addWidget(label(self.title, 'PageTitle'))
        head.addStretch(1)
        head.addWidget(pill('模型未接入', theme.PALETTE['amber'], '#FFF7E6', theme.PALETTE['amber']))
        self.content.addLayout(head)
        self.content.addWidget(label(self.subtitle, 'PageDesc'))

        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tiles: Dict[str, StatTile] = {}
        for key, name, levels, note in self.MODELS:
            tile = StatTile(name, '', '--', theme.PALETTE['text_dim'], note)
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
        for row, (_key, name, levels, note) in enumerate(self.MODELS):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(levels))
            self.table.setItem(row, 2, QTableWidgetItem('未接入'))
        detail.add(self.table)
        body.addWidget(detail, 3)

        bus = Card('总线上传')
        for name, value in (('地址', f'{cfg.server_host}:{cfg.server_port}'),
                            ('上行内容', '原始采样摘要 + 四个模型结果'),
                            ('状态', '未启用（等模型与总线协议确定）')):
            row = QHBoxLayout()
            row.addWidget(label(name, 'Dim'), 0)
            row.addWidget(label(str(value), 'PageDesc'), 1)
            bus.add_layout(row)
        hint = label('· 地址在 config.ini 的 [server] host / port，登录界面可临时覆盖；\n'
                     '· 上传协议（帧格式、鉴权）需要先与服务端约定，见 docs/需求拆解.md。',
                     'CardHint')
        hint.setWordWrap(True)
        bus.add(hint)
        body.addWidget(bus, 2)
        self.content.addLayout(body, 1)

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
    subtitle = '选择会话与时间段 → 特征提取（HRV / 脉搏；眼动与表情待模型接入）'

    def build(self) -> None:
        self.packets: List[tuple] = []
        self.bound: Tuple[float, float] = (0.0, 0.0)
        self.last_result: Optional[Dict[str, Any]] = None

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

        note_card = Card('说明')
        note = label(
            '· 心电：HRV（bpm / SDNN / RMSSD / pNN50 / SD1 / SD2 / VLF-LF-HF）；\n'
            '· 手环：PPG 交流耦合 → 峰值检测 → 脉率与脉间期；\n'
            '· 眼动、表情：模型未接入，输出 n/a；\n'
            '· 信号不可用（电极脱落 / 无接触）一律 n/a，不给假数值。', 'PageDesc')
        note.setWordWrap(True)
        note_card.add(note)
        side.addWidget(note_card, 1)
        body.addLayout(side, 2)
        self.content.addLayout(body, 1)

        self.refresh_sessions()

    # ------------------------------------------------------------------
    def refresh_sessions(self) -> None:
        self.session_combo.clear()
        for path, mtime, size in list_sessions(DATA_DIR):
            stamp = datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')
            self.session_combo.addItem(
                f'{os.path.basename(path)}   {stamp}   {size / 1024 / 1024:.1f} MB', path)
        if self.session_combo.count() == 0:
            self.session_combo.addItem('（data/ 下没有会话文件）', None)

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
        if not self.packets:
            self.load_selected()
            if not self.packets:
                return
        start, end = self._selected_range()
        result = analyze_packets(self.packets, start, end)
        self.last_result = result
        self._render(result)

    def _render(self, result: Dict[str, Any]) -> None:
        span = result['区间'][2]
        rows: List[Tuple[str, str, str]] = [
            ('分析时长', f'{span:.1f} 秒', '选定区间'),
        ]
        for section in ('心电', '手环PPG', '眼动', '表情'):
            block = result.get(section) or {}
            status = block.get('状态', 'n/a')
            rows.append((f'【{section}】', status, block.get('说明', '')))
            for name, value in (block.get('特征') or {}).items():
                rows.append((f'    {name}', 'n/a' if value is None else str(value), ''))

        self.result_table.setRowCount(len(rows))
        for index, (name, value, note) in enumerate(rows):
            self.result_table.setItem(index, 0, QTableWidgetItem(name))
            value_item = QTableWidgetItem(value)
            if name.startswith('【'):
                value_item.setForeground(Qt.darkGray)
            self.result_table.setItem(index, 1, value_item)
            self.result_table.setItem(index, 2, QTableWidgetItem(note))

        ecg = (result.get('心电') or {}).get('特征') or {}
        ppg = (result.get('手环PPG') or {}).get('特征') or {}
        self.tiles['hr'].set_value(str(ecg.get('bpm', '--')))
        self.tiles['pulse'].set_value(str(ppg.get('脉率', '--')))
        self.tiles['sdnn'].set_value(str(ecg.get('sdnn', '--')))
        self.tiles['lf_hf'].set_value(str(ecg.get('lf/hf', '--')))
        self.export_button.setEnabled(True)

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
