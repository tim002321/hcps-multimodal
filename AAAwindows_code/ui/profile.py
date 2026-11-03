# -*- coding: utf-8 -*-
"""登录 / 任务信息录入。

进入程序先弹这个对话框，把「谁、做什么任务、什么时候」定下来：

    人员姓名  +  任务名  +  时间   →   会话目录  data/<人员姓名>/<任务名>_<时间>/

**第一级目录是人员姓名**，同一个人做多次任务的数据会落在同一个文件夹下，
回放时先选人、再选那一次任务。

另外留了一个**总线上传地址**（IP），写入 `config.ini` 的 `[server] host`。

`Profile` 是纯数据，`ProfileDialog` 只负责收，两者都不碰采集逻辑，
所以命令行入口（main.py）也可以不带界面直接用 `Profile`。
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDateTimeEdit,
                             QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QRadioButton, QVBoxLayout)

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

# 登录界面专用样式：面向客户演示，字号比主界面再大一档
LOGIN_QSS = """
QDialog { background-color: #F2F5FA; }
QFrame#LoginCard {
    background-color: #FFFFFF; border: 1px solid #C9D6E8; border-radius: 10px;
}
QLabel#LoginTitle { font-size: 26pt; font-weight: bold; color: #1F5FA8; }
QLabel#LoginSub { font-size: 12pt; color: #6B6B6B; }
QLabel#LoginLabel { font-size: 14pt; color: #2B2B2B; }
QLineEdit {
    font-size: 15pt; padding: 10px 14px; min-height: 30px;
    border: 1px solid #B9C8DD; border-radius: 6px; background: #FFFFFF;
}
QDateTimeEdit {
    font-size: 15pt; padding: 10px 14px; padding-right: 34px; min-height: 30px;
    border: 1px solid #B9C8DD; border-radius: 6px; background: #FFFFFF;
}
QDateTimeEdit::drop-down { width: 30px; border: none; }
QLineEdit:focus, QDateTimeEdit:focus { border: 2px solid #2E6DB4; }
QRadioButton, QCheckBox { font-size: 13pt; spacing: 10px; }
QPushButton {
    font-size: 14pt; padding: 12px 34px; border-radius: 6px;
    border: 1px solid #B9C8DD; background: #FFFFFF;
}
QPushButton#Primary {
    background-color: #2E6DB4; color: #FFFFFF; border: 1px solid #1F5FA8;
    font-weight: bold;
}
QPushButton#Primary:hover { background-color: #3A7BD5; }
"""


@dataclass(frozen=True)
class Profile:
    """一次任务的身份信息。"""

    name: str                       # 人员姓名（第一级目录）
    task: str                       # 任务名
    started_at: datetime            # 时间（目录名后缀）
    server_host: str = '127.0.0.1'  # 总线上传地址
    # 这次要用哪些测量工具。键与 ui/replay.py 的 INSTRUMENTS 对齐。
    devices: Tuple[str, ...] = ('ecg', 'bracelet', 'camera')
    # 'record' 采集新数据 | 'review' 不进采集，直接看历史（回放 + 分析）
    mode: str = 'record'
    # 部署形态：'ground' 地面（本机） | 'host' 上机（上位机）
    host_mode: str = 'ground'

    @property
    def reviewing(self) -> bool:
        return self.mode == 'review'

    def session_subdir(self) -> str:
        """会话子目录：`<人员姓名>/<任务名>_<YYYYmmdd_HHMMSS>`。

        人员姓名做第一级文件夹，同一个人的多次任务都归在它下面。
        """
        return '{}/{}'.format(_safe(self.name), self.folder_name())

    def folder_name(self) -> str:
        return '{}_{}'.format(_safe(self.task), self.started_at.strftime('%Y%m%d_%H%M%S'))


def local_ip() -> str:
    """本机在局域网里的地址（登录界面只显示，不允许改）。

    用 UDP 连一下外部地址取默认出口 IP —— 不会真的发包，只是查路由表，
    所以没网也能拿到本机网卡地址；实在拿不到就退回主机名解析。
    """
    for probe in (('8.8.8.8', 80), ('223.5.5.5', 53)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.2)
                sock.connect(probe)
                address = sock.getsockname()[0]
            if address and not address.startswith('127.'):
                return address
        except OSError:
            continue
    try:
        address = socket.gethostbyname(socket.gethostname())
        if address:
            return address
    except OSError:
        pass
    return '127.0.0.1'


def _safe(text: str, fallback: str = '未命名') -> str:
    cleaned = ''.join(ch for ch in (text or '').strip() if ch not in '\\/:*?"<>|')
    return cleaned or fallback


class ProfileDialog(QDialog):
    """任务信息录入窗（登录界面）。全屏显示，字号按客户演示的尺寸放大。"""

    def __init__(self, cfg: Optional[Config] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('新建任务')
        self.setModal(True)
        # 全屏的前提是别被 sizeHint 压成一个小窗：给一个够大的最小尺寸，
        # 即使窗口管理器不认 showMaximized，界面也不会缩成巴掌大。
        self.setMinimumSize(theme.scaled(900), theme.scaled(640))
        self.setStyleSheet(theme.QSS + LOGIN_QSS)
        # IP 改成自动读取本机地址后，cfg 暂时用不上了（保留参数以兼容调用方）

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 32, 40, 32)
        root.addStretch(1)

        card = QFrame()
        card.setObjectName('LoginCard')
        card.setMaximumWidth(theme.scaled(1180))
        body = QVBoxLayout(card)
        body.setContentsMargins(64, 48, 64, 48)
        body.setSpacing(22)

        body.addWidget(label('新建任务', 'LoginTitle'))
        body.addWidget(label('填写本次采集的人员与任务信息', 'LoginSub'))

        mode_row = QHBoxLayout()
        mode_row.setSpacing(28)
        self.record_radio = QRadioButton('开始采集')
        self.record_radio.setChecked(True)
        self.review_radio = QRadioButton('查看历史数据')
        self.record_radio.toggled.connect(self._sync_mode)
        mode_row.addWidget(self.record_radio)
        mode_row.addWidget(self.review_radio)
        mode_row.addStretch(1)
        body.addLayout(mode_row)
        # 同一个窗口里有多组单选时，Qt 默认按 parent 互斥 —— 不分组的话
        # 点下面「上机」会把上面「开始采集」取消掉。
        self.mode_group = QButtonGroup(self)
        for button in (self.record_radio, self.review_radio):
            self.mode_group.addButton(button)

        form = QFormLayout()
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('姓名')
        form.addRow(self._form_label('人员姓名'), self.name_edit)

        self.task_edit = QLineEdit()
        self.task_edit.setPlaceholderText('任务名')
        form.addRow(self._form_label('任务名'), self.task_edit)

        self.time_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.time_edit.setDisplayFormat('yyyy-MM-dd HH:mm:ss')
        self.time_edit.setCalendarPopup(True)
        form.addRow(self._form_label('时间'), self.time_edit)

        host_row = QHBoxLayout()
        host_row.setSpacing(28)
        self.ground_radio = QRadioButton('地面')
        self.ground_radio.setChecked(True)
        self.host_radio = QRadioButton('上机')
        self.host_radio.toggled.connect(self._sync_host_mode)
        host_row.addWidget(self.ground_radio)
        host_row.addWidget(self.host_radio)
        host_row.addStretch(1)
        form.addRow(self._form_label('部署'), host_row)
        self.host_group = QButtonGroup(self)
        for button in (self.ground_radio, self.host_radio):
            self.host_group.addButton(button)

        self.ip_edit = QLineEdit(local_ip())
        self.ip_edit.setReadOnly(True)          # 只显示，不给改
        self.ip_edit.setToolTip('本机地址，自动读取，不可修改')
        self.ip_edit.setStyleSheet('background-color: #F0F0F0; color: #555555;')
        form.addRow(self._form_label('IP'), self.ip_edit)
        body.addLayout(form)

        self.host_hint = label('请确认 IP 地址和上位机的地址一致，可连接', 'LoginSub')
        self.host_hint.setStyleSheet('color: #C00000; font-size: 13pt;')
        self.host_hint.setVisible(False)
        body.addWidget(self.host_hint)

        device_row = QHBoxLayout()
        device_row.setSpacing(22)
        device_row.addWidget(self._form_label('测量工具'))
        self.device_boxes = {}
        for key, name, reserved in DEVICE_CHOICES:
            box = QCheckBox(name)
            box.setChecked(not reserved)
            if reserved:
                box.setToolTip('预留设备：页面可用，但当前版本不产生数据')
            self.device_boxes[key] = box
            device_row.addWidget(box)
        device_row.addStretch(1)
        body.addLayout(device_row)

        body.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button = QPushButton('开始')
        self.ok_button.setObjectName('Primary')
        self.ok_button.setMinimumWidth(theme.scaled(220))
        self.ok_button.clicked.connect(self._accept_if_valid)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.ok_button)
        body.addLayout(buttons)

        root.addWidget(card, 0, Qt.AlignHCenter)
        root.addStretch(1)

        self._sync_mode()

    @staticmethod
    def _form_label(text: str) -> QLabel:
        item = label(text, 'LoginLabel')
        item.setMinimumWidth(theme.scaled(120))
        return item

    # ------------------------------------------------------------------
    def _sync_mode(self) -> None:
        """查看历史时不需要填人员和任务，直接灰掉。"""
        reviewing = self.review_radio.isChecked()
        for widget in (self.name_edit, self.task_edit, self.time_edit):
            widget.setEnabled(not reviewing)
            widget.setStyleSheet(
                'color: #9A9A9A; background-color: #F0F0F0;' if reviewing else '')
        self.ok_button.setText('进入' if reviewing else '开始')

    def _sync_host_mode(self) -> None:
        """选「上机」时提醒确认 IP 与上位机一致。"""
        self.host_hint.setVisible(self.host_radio.isChecked())

    def _build(self) -> Profile:
        """收集窗口上的值。注意：**不回写 config.ini**——configparser 写文件会把
        里面的中文注释全丢掉，地址只在本次会话生效。"""
        qdt = self.time_edit.dateTime()
        started = datetime(qdt.date().year(), qdt.date().month(), qdt.date().day(),
                           qdt.time().hour(), qdt.time().minute(), qdt.time().second())
        devices = tuple(key for key, box in self.device_boxes.items() if box.isChecked())
        return Profile(
            name=self.name_edit.text().strip() or '临时',
            task=self.task_edit.text().strip() or '未命名任务',
            started_at=started,
            server_host=self.ip_edit.text().strip() or '127.0.0.1',
            devices=devices or ('ecg',),
            mode='review' if self.review_radio.isChecked() else 'record',
            host_mode='host' if self.host_radio.isChecked() else 'ground',
        )

    def _accept_if_valid(self) -> None:
        if self.review_radio.isChecked():
            self.accept()
            return
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        self.accept()

    # ------------------------------------------------------------------
    def profile(self) -> Profile:
        """对话框 accept 之后取结果。"""
        return self._build()

    @staticmethod
    def collect(cfg: Optional[Config] = None, parent=None) -> Optional[Profile]:
        """全屏弹出对话框；用户点「开始」返回 Profile，点取消返回 None。"""
        dialog = ProfileDialog(cfg, parent)
        screen = QApplication.desktop().availableGeometry()
        dialog.resize(screen.width(), screen.height())
        dialog.showMaximized()
        if dialog.exec_() == QDialog.Accepted:
            return dialog._build()
        return None
