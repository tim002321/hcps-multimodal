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
from typing import Optional

from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import (QDateTimeEdit, QDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QVBoxLayout)

from core.config import Config
from ui import theme
from ui.widgets import label


@dataclass(frozen=True)
class Profile:
    """一次实验的身份信息。"""

    name: str                       # 人物名
    group: str                      # 组别（作为一级文件夹）
    started_at: datetime            # 实验时间（作为目录名后缀）
    server_host: str = '127.0.0.1'  # 总线上传地址

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
        self.setMinimumWidth(460)
        self.setStyleSheet(theme.QSS)
        cfg = cfg or Config.load()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = label('开始一次新的采集', 'PageTitle')
        root.addWidget(title)
        hint = label('人物名 / 组别 / 实验时间将作为数据目录；总线地址用于上传实时结果。',
                     'PageDesc')
        hint.setWordWrap(True)
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
    def _current_subdir(self) -> str:
        return self._build().session_subdir()

    def _refresh_preview(self) -> None:
        self.preview.setText('数据目录：data/' + self._current_subdir())

    def _build(self) -> Profile:
        """收集窗口上的值。注意：**不回写 config.ini**——configparser 写文件会把
        里面的中文注释全丢掉，地址只在本次会话生效；要长期固定请直接改
        config.ini 的 [server] host（对话框每次启动会用那里的值预填）。"""
        qdt = self.time_edit.dateTime()
        started = datetime(qdt.date().year(), qdt.date().month(), qdt.date().day(),
                           qdt.time().hour(), qdt.time().minute(), qdt.time().second())
        return Profile(
            name=self.name_edit.text().strip() or '临时',
            group=self.group_edit.text().strip() or '未分组',
            started_at=started,
            server_host=self.ip_edit.text().strip() or '127.0.0.1',
        )

    def _accept_if_valid(self) -> None:
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
