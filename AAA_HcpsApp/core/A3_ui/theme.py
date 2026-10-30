# -*- coding: utf-8 -*-
"""A3 界面主题：科技感配色 + 全局 QSS。

配色思路：深空蓝底（近黑）+ 青色主色 + 少量琥珀/红做告警，
面板用半透明玻璃感 + 1px 冷色描边，数字用等宽字体，整体偏"仪表盘"。

颜色值在 PALETTE 里集中定义，QSS 里是字面量（QSS 的 `{` 太多，用 f-string 反而难读），
两边改动时要一起改。
"""
from __future__ import annotations

import os
from typing import Optional

# ---------------------------------------------------------------- 配色
PALETTE = {
    'bg': '#070B14',            # 窗口底
    'bg_soft': '#0B1220',       # 次级底（图表背景）
    'panel': '#111A2B',         # 面板
    'panel_alt': '#0E1626',     # 面板（更深）
    'line': '#1E2C45',          # 分隔线
    'line_soft': '#16233A',
    'text': '#E6F1FF',          # 主文字
    'text_dim': '#8AA0BF',      # 次要文字
    'text_faint': '#5B6E8C',    # 更弱的说明文字
    'cyan': '#22D3EE',          # 主色
    'cyan_dim': '#0E7490',
    'blue': '#3B82F6',
    'violet': '#8B5CF6',
    'green': '#34D399',
    'amber': '#F59E0B',
    'red': '#EF4444',
}

FONT_UI = 'Microsoft YaHei UI'
FONT_MONO = 'Consolas'

# 显式注册的系统字体文件（正常桌面 Qt 自己会加载；离屏渲染时字体库是空的，得手动来）
FONT_FILES = ['msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'consola.ttf', 'consolab.ttf', 'segoeui.ttf']

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')


def asset(name: str) -> str:
    """取 assets 下的素材绝对路径。"""
    return os.path.join(ASSETS_DIR, name)


def logo_path() -> Optional[str]:
    path = asset('logo.png')
    return path if os.path.exists(path) else None


def load_fonts() -> list:
    """把系统字体注册进 Qt 字体库，返回注册成功的文件名。

    为什么需要：`tools/screenshot_ui.py` 用 QT_QPA_PLATFORM=offscreen 离屏渲染，
    这时 Qt 的字体库是空的（`QFontDatabase().families()` 返回 0 个），
    界面能画出来但**一个字都不显示**。正常桌面环境本来就加载好了，多注册一次也无害。
    """
    from PyQt5.QtGui import QFontDatabase

    loaded = []
    font_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    for name in FONT_FILES:
        path = os.path.join(font_dir, name)
        if not os.path.exists(path):
            continue
        if QFontDatabase.addApplicationFont(path) >= 0:
            loaded.append(name)
    return loaded


def background_path() -> Optional[str]:
    path = asset('background.png')
    return path if os.path.exists(path) else None


QSS = """
QWidget {
    color: #E6F1FF;
    font-family: "Microsoft YaHei UI";
    font-size: 13px;
}
QMainWindow, #AppRoot {
    background-color: #070B14;
}

/* ---------------- 左侧导航 ---------------- */
#Sidebar {
    background-color: #0A0F1B;
    border-right: 1px solid #16233A;
}
#BrandName {
    color: #E6F1FF;
    font-size: 15px;
    font-weight: 600;
}
#BrandSub {
    color: #5B6E8C;
    font-size: 11px;
}
#NavSection {
    color: #4A5B78;
    font-size: 11px;
    letter-spacing: 1px;
    padding: 12px 18px 6px 18px;
}
QPushButton#NavItem {
    text-align: left;
    padding: 11px 16px;
    margin: 3px 10px;
    border: 1px solid transparent;
    border-radius: 8px;
    color: #8AA0BF;
    font-size: 14px;
    background: transparent;
}
QPushButton#NavItem:hover {
    background-color: #12203A;
    color: #E6F1FF;
    border: 1px solid #1E3A5F;
}
QPushButton#NavItem:checked {
    background-color: #10293F;
    color: #22D3EE;
    border: 1px solid #1C4A63;
    font-weight: 600;
}

/* ---------------- 顶部标题条 ---------------- */
#HeaderBar {
    background-color: #0A0F1B;
    border-bottom: 1px solid #16233A;
}
#HeaderTitle {
    font-size: 17px;
    font-weight: 600;
    color: #E6F1FF;
}
#HeaderSub {
    font-size: 11px;
    color: #5B6E8C;
}
#Pill {
    border-radius: 11px;
    padding: 5px 14px;
    font-size: 12px;
    background-color: #10293F;
    border: 1px solid #1C4A63;
    color: #22D3EE;
}

/* ---------------- 卡片 ---------------- */
#Card {
    background-color: #111A2B;
    border: 1px solid #1E2C45;
    border-radius: 10px;
}
#CardTitle {
    font-size: 14px;
    font-weight: 600;
    color: #E6F1FF;
}
#CardHint {
    font-size: 11px;
    color: #5B6E8C;
}
#StatValue {
    font-family: "Consolas";
    font-size: 26px;
    font-weight: 700;
    color: #22D3EE;
}
#StatUnit {
    font-size: 11px;
    color: #5B6E8C;
}
#StatLabel {
    font-size: 12px;
    color: #8AA0BF;
}
#StatLabelFaint {
    font-size: 11px;
    color: #5B6E8C;
}

/* ---------------- 文本 ---------------- */
#PageTitle {
    font-size: 20px;
    font-weight: 600;
    color: #E6F1FF;
}
#PageDesc {
    font-size: 12px;
    color: #8AA0BF;
}
#Mono {
    font-family: "Consolas";
    color: #9FDBEA;
}
#Dim {
    color: #5B6E8C;
}

/* ---------------- 按钮 ---------------- */
QPushButton#Primary {
    background-color: #0E7490;
    color: #EAFDFF;
    border: 1px solid #22D3EE;
    border-radius: 8px;
    padding: 9px 22px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#Primary:hover { background-color: #1597B8; }
QPushButton#Primary:pressed { background-color: #0B5F79; }
QPushButton#Primary:disabled {
    background-color: #16233A;
    border-color: #24344F;
    color: #4A5B78;
}
QPushButton#Ghost {
    background-color: rgba(34, 211, 238, 0.06);
    color: #9FDBEA;
    border: 1px solid #1E3A5F;
    border-radius: 8px;
    padding: 8px 18px;
}
QPushButton#Ghost:hover {
    background-color: rgba(34, 211, 238, 0.14);
    color: #E6F1FF;
    border: 1px solid #22D3EE;
}
QPushButton#Danger {
    background-color: rgba(239, 68, 68, 0.12);
    color: #FCA5A5;
    border: 1px solid #7F1D1D;
    border-radius: 8px;
    padding: 9px 22px;
    font-weight: 600;
}
QPushButton#Danger:hover { background-color: rgba(239, 68, 68, 0.22); }

/* ---------------- 输入 ---------------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit {
    background-color: #0B1220;
    border: 1px solid #1E2C45;
    border-radius: 7px;
    padding: 7px 10px;
    color: #E6F1FF;
    selection-background-color: #0E7490;
}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus {
    border: 1px solid #22D3EE;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #0E1626;
    border: 1px solid #1E2C45;
    selection-background-color: #10293F;
    selection-color: #22D3EE;
    outline: none;
}

/* ---------------- 表格 ---------------- */
QTableWidget, QTableView {
    background-color: #0B1220;
    alternate-background-color: #0E1626;
    gridline-color: #16233A;
    border: 1px solid #1E2C45;
    border-radius: 8px;
    selection-background-color: #10293F;
    selection-color: #22D3EE;
    outline: none;
}
QHeaderView::section {
    background-color: #0E1626;
    color: #8AA0BF;
    border: none;
    border-bottom: 1px solid #1E2C45;
    border-right: 1px solid #16233A;
    padding: 8px 10px;
    font-size: 12px;
}
QTableCornerButton::section { background-color: #0E1626; border: none; }

/* ---------------- 进度条（压力条） ---------------- */
QProgressBar {
    background-color: #0B1220;
    border: 1px solid #1E2C45;
    border-radius: 6px;
    text-align: center;
    color: #9FDBEA;
    font-family: "Consolas";
    font-size: 11px;
    height: 16px;
}
QProgressBar::chunk {
    border-radius: 5px;
    background-color: #22D3EE;
}

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px;
}
QScrollBar::handle:vertical {
    background: #1E2C45; border-radius: 5px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #22D3EE; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: #1E2C45; border-radius: 5px; min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: #22D3EE; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollArea { border: none; background: transparent; }

/* ---------------- 状态栏 ---------------- */
#StatusBar {
    background-color: #0A0F1B;
    border-top: 1px solid #16233A;
    color: #5B6E8C;
    font-size: 11px;
}
QToolTip {
    background-color: #0E1626;
    color: #E6F1FF;
    border: 1px solid #22D3EE;
    padding: 5px 8px;
}
"""
