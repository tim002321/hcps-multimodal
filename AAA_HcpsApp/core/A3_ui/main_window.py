# -*- coding: utf-8 -*-
"""A3 主窗口：左侧导航 + 顶部标题 + 六个页面 + 状态栏。

替代老工程 script.py 里 2000 行的 MyForm：

* 老：巨型窗口类、12 个全局 threading.Event 当开关、采集线程往 127.0.0.1:8008 发 UDP 回环、
  MDI 子窗口满天飞、路径写死。
* 新：界面只做显示和转发——采集交给 A0 的 CollectorManager（界面订阅它的 sink），
  落库交给 A1，分析交给 A2，界面里没有一行设备协议代码。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QStackedWidget,
                             QVBoxLayout, QWidget)
from PyQt5.QtCore import Qt

from core.A3_ui import theme
from core.A3_ui.pages import (AnalyzePage, BasePage, CollectPage, FacePage, GripPage,  # noqa: F401
                              OverviewPage, SettingsPage)
from core.A3_ui.widgets import LogBridge, NavItem, PacketBridge, dot, label, pill

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.A0_data_collect import CameraCollector, CollectorManager      # noqa: E402
from core.A0_data_collect.sim import SimCollector                      # noqa: E402
from core.A1_data_store import SqliteStore, usable_session_dirs        # noqa: E402
from core.A2_analyze import FaceExpressionAnalyzer, GripProcessor      # noqa: E402

log = logging.getLogger(__name__)

PAGE_SUBTITLES = {
    0: '实时链路状态与波形',
    1: '会话启停、设备状态、运行日志',
    2: '左右手压力与关节角',
    3: '会话库离线分析',
    4: 'CNN3 八类表情识别',
    5: '配置与模型资产',
}


def load_yaml(path: str) -> Dict[str, Any]:
    """读配置；没有 PyYAML 就当空配置（界面照样能起）。"""
    try:
        import yaml
    except ImportError:
        log.warning('没有 PyYAML，界面用空配置启动')
        return {}
    if not os.path.exists(path):
        log.warning('配置文件不存在：%s', path)
        return {}
    with open(path, encoding='utf-8') as handle:
        return yaml.safe_load(handle) or {}


class QtLogHandler(logging.Handler):
    """把 logging 接到界面的"运行日志"面板。

    只发 Qt 信号，不直接碰控件：日志可能是采集线程打的（A0 每个采集器一个线程），
    而控件只能在 GUI 线程改——直接 setHtml 会偶发访问违例（见 LogBridge 的说明）。
    """

    def __init__(self, bridge: LogBridge):
        super().__init__()
        self.bridge = bridge

    def emit(self, record):                           # noqa: D102
        try:
            self.bridge.emit_record(self.format(record), record.levelno)
        except Exception:                             # noqa: BLE001 - 日志不能反过来炸界面
            pass


class SessionController:
    """采集会话：A0 总控 + A1 存储 + 会话目录，界面按钮只调这里。"""

    def __init__(self, config: Dict[str, Any], bridge: PacketBridge, use_sim: bool = True):
        self.config = config
        self.bridge = bridge
        self.use_sim = use_sim
        self.store: Optional[SqliteStore] = None
        self.manager: Optional[CollectorManager] = None
        self.db_path = ''
        self.started_at: Optional[datetime] = None

    @property
    def running(self) -> bool:
        return self.store is not None

    def start(self, user: str, task: str, fresh: bool = False) -> str:
        session = self.config.get('session') or {}
        dirs = usable_session_dirs(user=user, task=task, root=session.get('root', '实验结果'),
                                   result_dir=session.get('result_dir', 'normal_result'),
                                   db_name=session.get('db_name', 'database.db'), fresh=fresh)
        self.db_path = dirs['db_path']
        self.store = SqliteStore(self.db_path).start()
        self.manager = CollectorManager.from_config(self.config, use_sim=self.use_sim,
                                                    session_dir=dirs['result_dir'])
        if self.use_sim:
            # from_config 的模拟源只有心电+眼动，握力页要看东西，这里补一路模拟手套
            self.manager.register(SimCollector({'kind': 'grip', 'name': 'sim_grip',
                                                'rate_hz': 10, 'device': 'grip'}))
        self.manager.add_sink(self.store.submit)      # 落库
        self.manager.add_sink(self.bridge)            # 界面实时曲线
        self.manager.start()
        self.started_at = datetime.now()
        return self.db_path

    def stop(self) -> None:
        if self.manager is not None:
            self.manager.stop()
            self.manager = None
        if self.store is not None:
            time.sleep(0.3)                           # 让分发队列排空
            self.store.stop()
            self.store = None

    def status(self) -> Dict[str, Any]:
        return self.manager.status() if self.manager is not None else {}

    @property
    def written(self) -> int:
        return self.store.written if self.store is not None else 0


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None, use_sim: bool = True,
                 config_path: Optional[str] = None):
        super().__init__()
        self.project_root = PROJECT_ROOT
        self.config_path = config_path or os.path.join(PROJECT_ROOT, 'config', 'default.yaml')
        self.config = config if config is not None else load_yaml(self.config_path)
        self.models_dir = os.path.join(PROJECT_ROOT, 'models')
        self.results_dir = os.path.join(PROJECT_ROOT, '实验结果')

        self.setWindowTitle('HcpsApp · 多模态数据采集与分析系统')
        self.resize(1520, 960)
        self.setStyleSheet(theme.QSS)
        self._closing = False

        self.grip_processor = GripProcessor(self.config.get('grip_process'))
        self.face_analyzer = FaceExpressionAnalyzer((self.config.get('analyze') or {}).get('face'))
        self.bridge = PacketBridge()
        self.bridge.packet.connect(self._on_packet)
        self.controller = SessionController(self.config, self.bridge, use_sim=use_sim)
        self.camera: Optional[CameraCollector] = None

        root = QWidget()
        root.setObjectName('AppRoot')
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._build_header())

        self.stack = QStackedWidget()
        self.pages: List[BasePage] = [OverviewPage(self), CollectPage(self), GripPage(self),
                                      AnalyzePage(self), FacePage(self), SettingsPage(self)]
        for page in self.pages:
            self.stack.addWidget(page)
        right_layout.addWidget(self.stack, 1)
        right_layout.addWidget(self._build_status_bar())
        layout.addWidget(right, 1)

        self._install_log_handler()
        self.switch_page(0)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._on_tick)
        self.tick_timer.start(1000)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()

    # ------------------------------------------------------------- 布局
    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(226)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 18, 0, 16)
        layout.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(18, 0, 16, 0)
        brand.setSpacing(10)
        logo = theme.logo_path()
        if logo:
            badge = QLabel()
            badge.setPixmap(QPixmap(logo).scaled(40, 40, Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
            brand.addWidget(badge)
        text_box = QVBoxLayout()
        text_box.setSpacing(0)
        text_box.addWidget(label('HcpsApp', 'BrandName'))
        text_box.addWidget(label('军事认知作业效能评估', 'BrandSub'))
        brand.addLayout(text_box)
        brand.addStretch(1)
        layout.addLayout(brand)
        layout.addSpacing(16)
        layout.addWidget(label('功能导航', 'NavSection'))

        self.nav_items: List[NavItem] = []
        entries = [('01', '总览'), ('02', '采集'), ('03', '握力'),
                   ('04', '分析'), ('05', '表情'), ('06', '设置')]
        for index, (badge, text) in enumerate(entries):
            item = NavItem(badge, text)
            item.clicked.connect(lambda _, i=index: self.switch_page(i))
            layout.addWidget(item)
            self.nav_items.append(item)
        layout.addStretch(1)

        footer = QVBoxLayout()
        footer.setContentsMargins(18, 0, 16, 0)
        footer.setSpacing(2)
        footer.addWidget(label('新壳（换壳保芯）', 'BrandSub'))
        footer.addWidget(label('A0 采集 / A1 存储', 'BrandSub'))
        footer.addWidget(label('A2 分析 / A3 界面', 'BrandSub'))
        layout.addLayout(footer)
        return sidebar

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName('HeaderBar')
        header.setFixedHeight(74)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(22, 0, 22, 0)
        layout.setSpacing(12)

        box = QVBoxLayout()
        box.setSpacing(2)
        self.header_title = label('总览', 'HeaderTitle')
        self.header_sub = label(PAGE_SUBTITLES[0], 'HeaderSub')
        box.addWidget(self.header_title)
        box.addWidget(self.header_sub)
        layout.addLayout(box)
        layout.addStretch(1)
        self.session_pill = pill('未开始采集', theme.PALETTE['text_dim'], '#0E1626', '#1E2C45')
        layout.addWidget(self.session_pill)
        self.clock_pill = pill('--:--:--')
        layout.addWidget(self.clock_pill)
        return header

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName('StatusBar')
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(22, 0, 22, 0)
        layout.setSpacing(10)
        self.status_dot = dot(theme.PALETTE['text_faint'])
        layout.addWidget(self.status_dot)
        self.status_text = label('就绪', 'BrandSub')
        layout.addWidget(self.status_text)
        layout.addStretch(1)
        layout.addWidget(label(PROJECT_ROOT, 'BrandSub'))
        return bar

    # ------------------------------------------------------------- 交互
    def start_camera(self, overrides: Optional[Dict[str, Any]] = None) -> CameraCollector:
        """打开本机摄像头（实时监测）。

        会话在跑 → 挂进总控，帧既进界面也落库（data_video_frame + jpg）；
        没会话 → 只把帧喂给界面，不落盘（避免污染库）。
        """
        if self.camera is not None:
            return self.camera
        config = dict((self.config.get('collectors') or {}).get('camera') or {})
        config.update(overrides or {})
        config['enabled'] = True
        if self.controller.running:
            config['session_dir'] = os.path.dirname(self.controller.db_path)
        else:
            config['save_frames'] = False             # 只监测
        camera = CameraCollector(config)
        if self.controller.running:
            self.controller.manager.register(camera)
        else:
            camera.set_sink(self.bridge)
        camera.start()
        self.camera = camera
        log.info('[camera] 实时监测已启动（会话%s）', '中，帧会落库' if self.controller.running else '未开始，帧不落盘')
        return camera

    def stop_camera(self) -> None:
        camera, self.camera = self.camera, None
        if camera is not None:
            camera.stop()
            log.info('[camera] 实时监测已停止（共 %s 帧，落盘 %s）', camera.camera_stats()['frames'],
                     camera.camera_stats()['saved'])

    def switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for position, item in enumerate(self.nav_items):
            item.setChecked(position == index)
        page = self.pages[index]
        self.header_title.setText(page.title)
        self.header_sub.setText(page.subtitle or PAGE_SUBTITLES.get(index, ''))

    def _on_packet(self, packet) -> None:
        if self._closing:
            return
        for page in self.pages:
            try:
                page.on_packet(packet)
            except Exception:                         # noqa: BLE001 - 单页面出错不影响其它页面
                log.exception('页面 %s 处理数据失败', page.title)

    def _on_tick(self) -> None:
        if self._closing:
            return
        for page in self.pages:
            try:
                page.on_tick()
            except Exception:                         # noqa: BLE001
                log.exception('页面 %s 刷新失败', page.title)
        running = self.controller.running
        self.status_dot.setStyleSheet(
            f'background-color: {theme.PALETTE["green"] if running else theme.PALETTE["text_faint"]};'
            f' border-radius: 4px;')
        self.status_text.setText(f'采集中 · 已入库 {self.controller.written} 条' if running else '就绪')
        if running:
            session = os.path.basename(os.path.dirname(os.path.dirname(self.controller.db_path)))
            self.session_pill.setText(f'会话 {session}')
        else:
            self.session_pill.setText('未开始采集')

    def _update_clock(self) -> None:
        self.clock_pill.setText(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def _install_log_handler(self) -> None:
        collect_page = self.pages[1]
        self.log_bridge = LogBridge(self)             # 接收者（self/页面）在 GUI 线程，Qt 自动排队
        self.log_bridge.message.connect(collect_page.append_log)
        handler = QtLogHandler(self.log_bridge)
        handler.setFormatter(logging.Formatter('%(asctime)s  %(message)s', '%H:%M:%S'))
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self.log_handler = handler

    def closeEvent(self, event):                      # noqa: N802 - Qt 命名
        # 顺序有讲究：先摘日志处理器（采集线程可能还在打日志），再停采集，最后关子进程，
        # 这样销毁控件时不会再有别的线程往回写信号。
        self._closing = True
        logging.getLogger().removeHandler(self.log_handler)
        if self.controller.running:
            self.controller.stop()
        self.stop_camera()
        face_page = self.pages[4]
        if hasattr(face_page, 'shutdown'):
            face_page.shutdown()                      # 关掉表情识别子进程
        super().closeEvent(event)


def create_app(argv: Optional[List[str]] = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or [])
    app.setStyle('Fusion')
    theme.load_fonts()                                # 离屏渲染时字体库是空的，得手动注册
    app.setFont(QFont(theme.FONT_UI, 10))             # 保证图表的坐标轴文字也有字体可用
    return app


def run(use_sim: bool = True, config_path: Optional[str] = None) -> int:
    """起界面（run_ui.py 会调这里）。"""
    app = create_app(sys.argv)
    config = load_yaml(config_path or os.path.join(PROJECT_ROOT, 'config', 'default.yaml'))
    window = MainWindow(config, use_sim=use_sim, config_path=config_path)
    window.show()
    return app.exec_()
