# -*- coding: utf-8 -*-
"""登录 / 被试信息录入。

进入程序先弹这个对话框，采集前把「谁、哪一组、什么时候」定下来：

    人物名  +  组别  +  实验时间   →   会话目录  data/<组别>/<人物名>_<时间>/

另外留了一个**总线上传地址**（IP），写入 `config.ini` 的 `[server] host`，
后续实时数据与四个模型的结果都往这个地址发。

`Profile` 是纯数据，`ProfileDialog` 只负责收，两者都不碰采集逻辑，
所以命令行入口（main.py）也可以不带界面直接用 `Profile`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import (QCheckBox, QDateTimeEdit, QDialog, QFormLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout)

from core.config import Config
from ui import theme
from ui.widgets import label

# 可选的测量工具：键与 ui/replay.py 的 INSTRUMENTS 一致。
# 第三列 = 是否「预留设备」：预留的也能勾（进去看得到页面），但采不到数据，
# 所以采集段的完成判定会把它排除在外 —— 全选预留设备时直接允许跳过采集。
DEVICE_CHOICES = [
    ('ecg', '心电贴', False),
    ('bracelet', '手环', False),
    ('camera', '摄像头', False),
    ('emg', '表面肌电', True),
    ('eye', '眼动仪', True),
]

# 真正能产出数据的设备；其余都是预留
DATA_DEVICES = ('ecg', 'bracelet', 'camera')


@dataclass(frozen=True)
class Profile:
    """一次实验的身份信息。"""

    name: str                       # 人物名
    group: str                      # 组别（作为一级文件夹）
    started_at: datetime            # 实验时间（作为目录名后缀）
    server_host: str = '127.0.0.1'  # 总线上传地址
    # 这次要用哪些测量工具。键与 ui/replay.py 的 INSTRUMENTS 对齐。
    devices: Tuple[str, ...] = ('ecg', 'bracelet', 'camera')
    # 'record' 采集新数据 | 'review' 不进采集，直接看历史（回放 + 分析）
    mode: str = 'record'

    @property
    def reviewing(self) -> bool:
        return self.mode == 'review'

    def session_subdir(self) -> str:
        """会话子目录：`<组别>/<人物名>_<YYYYmmdd_HHMMSS>`。

        姓名和组别会做最基本的文件名清洗（去掉路径分隔符等非法字符），
        避免有人输入 `A/B` 时把目录建歪。
        """
        return '{}/{}'.format(_safe(self.group), self.folder_name())

    def folder_name(self) -> str:
        return '{}_{}'.format(_safe(self.name), self.started_at.strftime('%Y%m%d_%H%M%S'))


def _safe(text: str, fallback: str = '未命名') -> str:
    cleaned = ''.join(ch for ch in (text or '').strip() if ch not in '\\/:*?"<>|')
    return cleaned or fallback


class ProfileDialog(QDialog):
    """被试信息录入窗（登录界面）。"""

    def __init__(self, cfg: Optional[Config] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('实验信息录入')
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.QSS)
        cfg = cfg or Config.load()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = label('本次使用', 'PageTitle')
        root.addWidget(title)

        # 先定「干什么」：采集新数据，还是直接翻历史做回放/分析。
        # 这个选择决定下面的字段是不是必填（回看历史不需要人物名）。
        mode_row = QHBoxLayout()
        mode_row.setSpacing(16)
        self.record_radio = QRadioButton('开始采集')
        self.record_radio.setChecked(True)
        self.review_radio = QRadioButton('直接查看历史（回放 / 分析）')
        self.record_radio.toggled.connect(self._sync_mode)
        mode_row.addWidget(self.record_radio)
        mode_row.addWidget(self.review_radio)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        hint = label('人物名 / 组别 / 实验时间将作为数据目录', 'PageDesc')
        hint.setWordWrap(True)
        self.hint = hint
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('例如 张三')
        form.addRow('人物名', self.name_edit)

        self.group_edit = QLineEdit()
        self.group_edit.setPlaceholderText('例如 A组 / 对照组')
        form.addRow('组别', self.group_edit)

        self.time_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.time_edit.setDisplayFormat('yyyy-MM-dd HH:mm:ss')
        self.time_edit.setCalendarPopup(True)
        form.addRow('实验时间', self.time_edit)

        self.ip_edit = QLineEdit(cfg.server_host)
        self.ip_edit.setPlaceholderText('总线上传地址，例如 192.168.1.50')
        form.addRow('总线 IP', self.ip_edit)
        root.addLayout(form)

        # 测量工具：选中的设备决定后面显示哪些导航页（预留项置灰）
        device_row = QHBoxLayout()
        device_row.setSpacing(12)
        device_row.addWidget(label('测量工具', 'Dim'))
        self.device_boxes = {}
        for key, name, reserved in DEVICE_CHOICES:
            box = QCheckBox(name)
            box.setChecked(not reserved)
            box.setEnabled(True)                  # 全部可点，预留的也能进去看
            if reserved:
                box.setToolTip('预留设备：页面可用，但当前版本不产生数据，'
                               '只选它时采集段允许直接跳过')
            self.device_boxes[key] = box
            device_row.addWidget(box)
        device_row.addStretch(1)
        root.addLayout(device_row)
        self.device_hint = label('肌电 / 眼动仪为预留设备：页面能进，但当前不产生数据', 'CardHint')
        self.device_hint.setWordWrap(True)
        root.addWidget(self.device_hint)

        self.preview = label('', 'CardHint')
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)
        self.name_edit.textChanged.connect(self._refresh_preview)
        self.group_edit.textChanged.connect(self._refresh_preview)
        self.time_edit.dateTimeChanged.connect(self._refresh_preview)
        self._refresh_preview()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton('跳过（临时会话）')
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button = QPushButton('开始')
        self.ok_button.setObjectName('Primary')
        self.ok_button.clicked.connect(self._accept_if_valid)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.ok_button)
        root.addLayout(buttons)

    # ------------------------------------------------------------------
    def _sync_mode(self) -> None:
        """采集模式要人物名，回看历史不需要 —— 顺带把无意义的字段灰掉。

        注意光 setEnabled(False) 是不够的：QSS 里没有写 :disabled 的配色，
        禁用后输入框看上去还是白底黑字，用户会一直点它。所以这里直接给一层
        局部样式，把"不可用"这件事画出来。
        """
        reviewing = self.review_radio.isChecked()
        for widget in (self.name_edit, self.group_edit, self.time_edit):
            widget.setEnabled(not reviewing)
            widget.setStyleSheet(
                'color: #9A9A9A; background-color: #F0F0F0;' if reviewing else '')
        self.preview.setVisible(not reviewing)
        if reviewing:
            self.hint.setText('只看历史数据：下一步进入「会话与回放」，不再新建会话目录')
            self.ok_button.setText('进入')
        else:
            self.hint.setText('人物名 / 组别 / 实验时间将作为数据目录')
            self.ok_button.setText('开始')

    def _current_subdir(self) -> str:
        return self._build().session_subdir()

    def _refresh_preview(self) -> None:
        if self.review_radio.isChecked():
            return
        self.preview.setText('数据目录：data/' + self._current_subdir())

    def _build(self) -> Profile:
        """收集窗口上的值。注意：**不回写 config.ini**——configparser 写文件会把
        里面的中文注释全丢掉，地址只在本次会话生效；要长期固定请直接改
        config.ini 的 [server] host（对话框每次启动会用那里的值预填）。"""
        qdt = self.time_edit.dateTime()
        started = datetime(qdt.date().year(), qdt.date().month(), qdt.date().day(),
                           qdt.time().hour(), qdt.time().minute(), qdt.time().second())
        devices = tuple(key for key, box in self.device_boxes.items() if box.isChecked())
        return Profile(
            name=self.name_edit.text().strip() or '临时',
            group=self.group_edit.text().strip() or '未分组',
            started_at=started,
            server_host=self.ip_edit.text().strip() or '127.0.0.1',
            devices=devices or ('ecg',),
            mode='review' if self.review_radio.isChecked() else 'record',
        )

    def _accept_if_valid(self) -> None:
        if self.review_radio.isChecked():
            self.accept()
            return
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            self.preview.setText('请先填写人物名（会作为目录名，不能为空）')
            return
        self.accept()

    # ------------------------------------------------------------------
    def profile(self) -> Profile:
        """对话框 accept 之后取结果。"""
        return self._build()

    @staticmethod
    def collect(cfg: Optional[Config] = None, parent=None) -> Optional[Profile]:
        """弹出对话框；用户点「开始」返回 Profile，点跳过返回 None（临时会话）。"""
        dialog = ProfileDialog(cfg, parent)
        if dialog.exec_() == QDialog.Accepted:
            return dialog._build()
        return None
