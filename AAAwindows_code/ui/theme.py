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
import re
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

# ---------------------------------------------------------------------------
# 字号与尺寸
# ---------------------------------------------------------------------------
# 字号一律用 **pt（点）**，不用 px：
#   pt 是相对单位 —— Qt 按屏幕的逻辑 DPI 把它换算成像素，所以「9pt 正文」
#   在 96dpi 屏上画成 12px、在 144dpi 屏上画成 18px，物理尺寸一致。
#   写 px 则相反：12px 永远是 12 个逻辑像素，在 150% 缩放的屏上就显得很小
#   （这就是之前"字太小"的直接原因，见 ui/window.py 的 create_app）。
#
#   注：Qt 的 QSS 不支持 em / rem 作为 font-size（实测会回退成继承值），
#   所以"相对字号"在 Qt 里只有 pt 这一种正确写法。
#
#   FONT_SCALE 是**显示偏好**倍率（不是 DPI 适配）：默认 1.0，
#   觉得还不够大就用命令行或配置再乘一档，pt 值会整体等比放大。
#       python run_monitor.py --ui-scale 1.15
#       set HCP_UI_SCALE=1.15        （或 config.ini 的 [ui] scale）
FONT_SCALE = 1.25   # 面向客户演示：整体字号比原始设计放大 25%

# 字号阶梯（pt）。括号里是它在标准 96dpi 屏上的等效像素，方便和旧值对照。
FS_SMALL = 8.25        # 次要说明 / 单位 / 表头（≈11px）
FS_BASE = 9.0          # 正文（≈12px）
FS_NAV = 9.75          # 左侧导航按钮（≈13px）
FS_SUB = 10.5          # 数值块副标题 / 曲线当前值（≈14px）
FS_TITLE = 12.0        # 页面标题 / 顶部标题（≈16px）
FS_METRIC = 15.0       # 大号数字（≈20px）

# 曲线坐标轴刻度字号（pt）。pyqtgraph 不吃 QSS，必须单独传给 AxisItem。
PLOT_TICK_PT = 9.0


def pt(value: float) -> str:
    """字号 → 当前倍率下的 `Npt` 字符串（QSS / 内联样式用）。"""
    return '{}pt'.format(round(float(value) * FONT_SCALE, 2))


def scaled(value: float) -> int:
    """固定宽高按倍率换算。

    这里刻意保留 px（逻辑像素）而不是 pt：布局尺寸该随屏幕缩放走（dp 语义），
    不该随字号倍率以外的字体 DPI 变化；倍数只用来跟住 FONT_SCALE，
    避免字放大后被容器裁切。
    """
    return max(1, int(round(float(value) * FONT_SCALE)))


def scaled_pt(value: float) -> float:
    """字号用的 pt 数值（给 QFont.setPointSizeF 这类接口用）。"""
    return round(float(value) * FONT_SCALE, 2)



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


_QSS_TEMPLATE = """
/* 字号全部用 pt（相对单位，按屏幕逻辑 DPI 换算）；括号内为 96dpi 屏的等效像素 */
QWidget { color: #1F1F1F; font-family: "Microsoft YaHei"; font-size: 9pt; }      /* ≈12px */
QMainWindow, #AppRoot { background-color: #ECECEC; }

/* ---------------- 左侧导航：蓝色块 ---------------- */
#Sidebar { background-color: #F5F5F5; border-right: 1px solid #A9A9A9; }
#BrandName { font-size: 10.5pt; font-weight: bold; color: #1F5FA8; }             /* ≈14px */
#BrandSub { font-size: 8.25pt; color: #666666; }                                 /* ≈11px */
#NavSection {
    color: #FFFFFF; background-color: #2E6DB4; font-weight: bold;
    padding: 5px 10px; border: 1px solid #1F5FA8;
}
QPushButton#NavItem {
    text-align: left; padding: 8px 12px; margin: 1px 8px;
    background-color: #2E6DB4; color: #FFFFFF;
    border: 1px solid #22568F; border-radius: 2px; font-size: 9.75pt;            /* ≈13px */
}
QPushButton#NavItem:hover { background-color: #3A7BD5; }
QPushButton#NavItem:checked { background-color: #1F5FA8; border: 2px solid #17497F; font-weight: bold; }

/* ---------------- 三段式阶段条 / 向导条 ---------------- */
#StageBar { background-color: #EEF3FA; border-bottom: 1px solid #A9A9A9; }
QPushButton#StageChip {
    padding: 6px 16px; border: 1px solid #9DB7D8; border-radius: 14px;
    background-color: #FFFFFF; color: #4A4A4A; font-size: 9.75pt;
}
QPushButton#StageChip:hover { background-color: #E8F0FB; }
QPushButton#StageChip:checked {
    background-color: #2E6DB4; color: #FFFFFF; border: 1px solid #17497F; font-weight: bold;
}
/* 未解锁的阶段：灰底灰字，一眼看出"还不能进" */
QPushButton#StageChip:disabled {
    background-color: #E4E4E4; color: #9A9A9A; border: 1px dashed #B8B8B8;
}
#StageArrow { color: #9DB7D8; font-size: 12pt; }

#WizardBar { background-color: #FFFFFF; border-top: 1px solid #A9A9A9; }

QTabWidget#AcqTabs::pane { border: 1px solid #C9D6E8; }
QTabBar::tab {
    padding: 6px 18px; margin-right: 2px; border: 1px solid #C9D6E8;
    border-bottom: none; background-color: #F0F4FA; color: #4A4A4A;
}
QTabBar::tab:selected { background-color: #FFFFFF; color: #1F5FA8; font-weight: bold; }
QTabBar::tab:hover { background-color: #E4EDF9; }

/* ---------------- 标题条 ---------------- */
#HeaderBar { background-color: #FFFFFF; border-bottom: 1px solid #A9A9A9; }
#HeaderTitle { font-size: 12pt; font-weight: bold; color: #1F5FA8; }             /* ≈16px */
#HeaderSub { font-size: 8.25pt; color: #666666; }                                /* ≈11px */
#Pill {
    border-radius: 2px; padding: 3px 10px; font-size: 8.25pt;                    /* ≈11px */
    background-color: #EAF2FB; border: 1px solid #9DBBDD; color: #1F5FA8;
}

/* ---------------- 面板 / 分区条 ---------------- */
#Card { background-color: #FFFFFF; border: 1px solid #A9A9A9; border-radius: 0; }
#SectionBar {
    background-color: #2E6DB4; color: #FFFFFF; font-weight: bold;
    border: 1px solid #1F5FA8; padding: 4px 8px; font-size: 9pt;                 /* ≈12px */
}
#CardTitle { font-size: 9pt; font-weight: bold; color: #1F1F1F; }                /* ≈12px */
#CardHint { font-size: 8.25pt; color: #777777; }                                 /* ≈11px */
#StatValue { font-family: "Consolas"; font-size: 15pt; font-weight: bold; color: #C00000; }  /* ≈20px */
#StatUnit { font-size: 8.25pt; color: #666666; }                                 /* ≈11px */
#StatLabel { font-size: 9pt; color: #333333; }                                   /* ≈12px */
#StatLabelFaint { font-size: 8.25pt; color: #777777; }                           /* ≈11px */
#PageTitle { font-size: 12pt; font-weight: bold; color: #1F5FA8; }               /* ≈16px */
#PageDesc { font-size: 8.25pt; color: #666666; }                                 /* ≈11px */
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
    padding: 4px 6px; font-size: 8.25pt;                                         /* ≈11px */
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
#StatusBar { background: #F5F5F5; border-top: 1px solid #A9A9A9; color: #555555; font-size: 8.25pt; }  /* ≈11px */
QToolTip { background: #FFFFE1; color: #1F1F1F; border: 1px solid #7F7F7F; padding: 4px 6px; }
QCheckBox { color: #1F1F1F; }
"""


def _apply_scale(qss: str) -> str:
    """把模板里所有 `font-size: Npt` 按 FONT_SCALE 重算。

    只动字号，不动 padding/border —— 布局尺寸由各处的 theme.scaled() 单独处理，
    这样调倍率时不会顺带把颜色、边框、间距一起改掉。
    """
    return re.sub(r'font-size:\s*(\d+(?:\.\d+)?)pt',
                  lambda match: 'font-size: ' + pt(match.group(1)), qss)


QSS = _apply_scale(_QSS_TEMPLATE)


def set_scale(value: float) -> float:
    """设置全局字号倍率并重建 QSS。必须在创建任何窗口/对话框之前调用。"""
    global FONT_SCALE, QSS
    FONT_SCALE = max(0.8, min(3.0, float(value)))
    QSS = _apply_scale(_QSS_TEMPLATE)
    return FONT_SCALE
