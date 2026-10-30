# -*- coding: utf-8 -*-
"""界面主题：浅色工业风（白底 + 蓝色分区条 + 黑色绘图区）。

参照现场"生理心理训练模拟器—数据监控"那套界面的视觉：
  * 窗口浅灰、面板白色、1px 灰色描边，信息密度高；
  * 分区标题用蓝色横条（白字加粗）；
  * 左侧导航是一列蓝色块，选中项加深；
  * 曲线画在黑色画布上，坐标轴浅灰，带一条黄色竖向游标；
  * 右侧"待选参数 / 已选参数"双列表 + 添加/删除。
"""
from __future__ import annotations

import os
from typing import List, Optional

PALETTE = {
    'bg': '#ECECEC',
    'panel': '#FFFFFF',
    'panel_alt': '#F5F5F5',
    'line': '#A9A9A9',
    'line_soft': '#C8C8C8',
    'text': '#1F1F1F',
    'text_dim': '#555555',
    'text_faint': '#777777',
    'blue': '#2E6DB4',
    'blue_dark': '#1F5FA8',
    'blue_light': '#3A7BD5',
    'cyan': '#0070C0',
    'green': '#1E8E3E',
    'amber': '#B8860B',
    'red': '#C00000',
    'plot_bg': '#101010',
    'plot_grid': '#333333',
    'plot_text': '#C8C8C8',
}

FONT_UI = 'Microsoft YaHei'
FONT_MONO = 'Consolas'
FONT_FILES = ['msyh.ttc', 'msyhbd.ttc', 'simsun.ttc', 'consola.ttf', 'segoeui.ttf']
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')


def asset(name: str) -> str:
    return os.path.join(ASSETS_DIR, name)


def logo_path() -> Optional[str]:
    path = asset('logo.png')
    return path if os.path.exists(path) else None


def load_fonts() -> List[str]:
    """显式注册系统字体（离屏截图时 Qt 字体库为空，不注册会一个字都不显示）。"""
    from PyQt5.QtGui import QFontDatabase

    loaded = []
    font_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    for name in FONT_FILES:
        path = os.path.join(font_dir, name)
        if os.path.exists(path) and QFontDatabase.addApplicationFont(path) >= 0:
            loaded.append(name)
    return loaded


QSS = """
QWidget { color: #1F1F1F; font-family: "Microsoft YaHei"; font-size: 12px; }
QMainWindow, #AppRoot { background-color: #ECECEC; }

/* ---------------- 左侧导航：蓝色块 ---------------- */
#Sidebar { background-color: #F5F5F5; border-right: 1px solid #A9A9A9; }
#BrandName { font-size: 14px; font-weight: bold; color: #1F5FA8; }
#BrandSub { font-size: 11px; color: #666666; }
#NavSection {
    color: #FFFFFF; background-color: #2E6DB4; font-weight: bold;
    padding: 5px 10px; border: 1px solid #1F5FA8;
}
QPushButton#NavItem {
    text-align: left; padding: 8px 12px; margin: 1px 8px;
    background-color: #2E6DB4; color: #FFFFFF;
    border: 1px solid #22568F; border-radius: 2px; font-size: 13px;
}
QPushButton#NavItem:hover { background-color: #3A7BD5; }
QPushButton#NavItem:checked { background-color: #1F5FA8; border: 2px solid #17497F; font-weight: bold; }

/* ---------------- 标题条 ---------------- */
#HeaderBar { background-color: #FFFFFF; border-bottom: 1px solid #A9A9A9; }
#HeaderTitle { font-size: 16px; font-weight: bold; color: #1F5FA8; }
#HeaderSub { font-size: 11px; color: #666666; }
#Pill {
    border-radius: 2px; padding: 3px 10px; font-size: 11px;
    background-color: #EAF2FB; border: 1px solid #9DBBDD; color: #1F5FA8;
}

/* ---------------- 面板 / 分区条 ---------------- */
#Card { background-color: #FFFFFF; border: 1px solid #A9A9A9; border-radius: 0; }
#SectionBar {
    background-color: #2E6DB4; color: #FFFFFF; font-weight: bold;
    border: 1px solid #1F5FA8; padding: 4px 8px; font-size: 12px;
}
#CardTitle { font-size: 12px; font-weight: bold; color: #1F1F1F; }
#CardHint { font-size: 11px; color: #777777; }
#StatValue { font-family: "Consolas"; font-size: 20px; font-weight: bold; color: #C00000; }
#StatUnit { font-size: 11px; color: #666666; }
#StatLabel { font-size: 12px; color: #333333; }
#StatLabelFaint { font-size: 11px; color: #777777; }
#PageTitle { font-size: 16px; font-weight: bold; color: #1F5FA8; }
#PageDesc { font-size: 11px; color: #666666; }
#Mono { font-family: "Consolas"; color: #1F1F1F; }
#Dim { color: #555555; }

/* ---------------- 按钮 ---------------- */
QPushButton {
    background-color: #F0F0F0; color: #1F1F1F;
    border: 1px solid #A9A9A9; border-radius: 2px; padding: 4px 12px;
}
QPushButton:hover { background-color: #E4EEF8; border: 1px solid #7FA8D0; }
QPushButton:pressed { background-color: #D6E6F5; }
QPushButton#Primary {
    background-color: #2E6DB4; color: #FFFFFF; border: 1px solid #1F5FA8;
    font-weight: bold; padding: 5px 16px;
}
QPushButton#Primary:hover { background-color: #3A7BD5; }
QPushButton#Primary:disabled { background-color: #C8C8C8; border-color: #B0B0B0; color: #F0F0F0; }
QPushButton#Ghost { background-color: #F7F7F7; }
QPushButton#Danger { background-color: #C00000; color: #FFFFFF; border: 1px solid #8B0000; font-weight: bold; }

/* ---------------- 输入 ---------------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #FFFFFF; border: 1px solid #A9A9A9; border-radius: 1px;
    padding: 3px 6px; color: #1F1F1F;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #2E6DB4; }
QComboBox QAbstractItemView { background: #FFFFFF; border: 1px solid #A9A9A9;
    selection-background-color: #D6E6F5; selection-color: #1F1F1F; }

/* ---------------- 表格 ---------------- */
QTableWidget, QTableView {
    background-color: #FFFFFF; alternate-background-color: #F7FAFD;
    gridline-color: #C8C8C8; border: 1px solid #A9A9A9;
    selection-background-color: #D6E6F5; selection-color: #1F1F1F; outline: none;
}
QHeaderView::section {
    background-color: #E8EEF5; color: #1F1F1F; font-weight: bold;
    border: none; border-bottom: 1px solid #A9A9A9; border-right: 1px solid #C8C8C8;
    padding: 4px 6px; font-size: 11px;
}
QTableCornerButton::section { background-color: #E8EEF5; border: none; }

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical { background: #F0F0F0; width: 14px; margin: 0; border-left: 1px solid #D0D0D0; }
QScrollBar::handle:vertical { background: #C8C8C8; min-height: 26px; border: 1px solid #A9A9A9; }
QScrollBar::handle:vertical:hover { background: #B0B0B0; }
QScrollBar:horizontal { background: #F0F0F0; height: 14px; margin: 0; border-top: 1px solid #D0D0D0; }
QScrollBar::handle:horizontal { background: #C8C8C8; min-width: 26px; border: 1px solid #A9A9A9; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollArea { border: none; background: transparent; }

/* ---------------- 状态栏 ---------------- */
#StatusBar { background: #F5F5F5; border-top: 1px solid #A9A9A9; color: #555555; font-size: 11px; }
QToolTip { background: #FFFFE1; color: #1F1F1F; border: 1px solid #7F7F7F; padding: 4px 6px; }
QCheckBox { color: #1F1F1F; }
"""
