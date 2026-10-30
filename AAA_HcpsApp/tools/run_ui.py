# -*- coding: utf-8 -*-
"""启动新界面（A3）。

用法：
    python tools/run_ui.py                 # 模拟源，无硬件也能看全部页面
    python tools/run_ui.py --auto          # 起来就自动开始采集（模拟源）
    python tools/run_ui.py --real --auto   # 按 config/default.yaml 起真实采集器
    python tools/run_ui.py --config D:\\x\\default.yaml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A3_ui import MainWindow, create_app                              # noqa: E402
from core.A3_ui.main_window import load_yaml                               # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='HcpsApp 界面')
    parser.add_argument('--config', default=str(ROOT / 'config' / 'default.yaml'))
    parser.add_argument('--real', action='store_true', help='用真实设备（默认模拟源）')
    parser.add_argument('--auto', action='store_true', help='启动后自动开始采集')
    parser.add_argument('--user', default='smoke')
    parser.add_argument('--task', default='ui')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    config = load_yaml(args.config)
    app = create_app(sys.argv)
    window = MainWindow(config, use_sim=not args.real)
    window.show()
    if args.auto:
        window.pages[1].user_edit.setText(args.user)
        window.pages[1].task_edit.setText(args.task)
        window.pages[1]._start(fresh=False)
        window.switch_page(0)
    return app.exec_()


if __name__ == '__main__':
    os.environ.setdefault('QT_AUTO_SCREEN_SCALE_FACTOR', '1')
    raise SystemExit(main())
