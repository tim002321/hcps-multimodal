# -*- coding: utf-8 -*-
"""GUI 入口：python run_gui.py

打包后 __file__ 在 _MEIPASS（临时目录，退出即删），所以错误日志必须写到 exe 旁边，
否则崩溃时什么都不留，无从排查。
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from facetool.tv_stub import install as _install_tv_stub
    _install_tv_stub()
except Exception:  # noqa: BLE001
    pass



# 打包后关掉 ultralytics 的在线检查，避免离线环境卡在联网上
os.environ.setdefault('YOLO_VERBOSE', 'False')
os.environ.setdefault('ULTRALYTICS_OFFLINE', 'True')


def log_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    try:
        from facetool.gui.app import main as run
        return run()
    except Exception:                                  # noqa: BLE001
        text = traceback.format_exc()
        try:
            with open(os.path.join(log_dir(), 'gui_error.log'), 'w', encoding='utf-8') as fp:
                fp.write(text)
        except OSError:
            pass
        try:
            sys.stderr.write(text)
        except Exception:                              # noqa: BLE001
            pass
        raise


if __name__ == '__main__':
    sys.exit(main())
