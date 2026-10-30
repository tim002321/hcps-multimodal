# -*- coding: utf-8 -*-
"""A3 界面层（新壳）：PyQt5 + pyqtgraph，深色科技风。

* theme.py        配色 + 全局 QSS + 素材路径（校徽/实验室照片在 assets/）
* widgets.py      通用控件：卡片、大数字块、导航项、图表面板、自绘压力条、背景
* pages.py        六个页面：总览 / 采集 / 握力 / 分析 / 表情 / 设置
* main_window.py  主窗口 + 采集会话控制（SessionController）

界面里没有设备协议、没有数据库写语句：采集由 A0 的 CollectorManager 负责（界面订阅 sink），
落库走 A1，分析走 A2。启动见 `tools/run_ui.py`。
"""
from core.A3_ui.main_window import MainWindow, SessionController, create_app, run   # noqa: F401

__all__ = ['MainWindow', 'SessionController', 'create_app', 'run']
