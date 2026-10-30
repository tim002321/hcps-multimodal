# -*- coding: utf-8 -*-
"""启动前的依赖自检。

存在意义：这个工程必须用项目自带的 `.venv` 解释器跑。PyCharm 新建 Run Configuration
时**默认不继承**项目解释器，很容易拿系统 Python 一跑就报

    ModuleNotFoundError: No module named 'PyQt5'

光看这一行很难判断是"没装"还是"装到别的解释器里了"，所以这里提前检查一遍，
把**当前用的解释器路径**和**应该用的解释器路径**都打出来。

用法（两个入口文件顶部调用）：
    from core.preflight import check_dependencies
    check_dependencies(need_ui=True)
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Iterable, List

# 项目根：本文件在 core/ 下
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 用于展示的原始大小写路径
EXPECTED_PYTHON = os.path.join(os.path.dirname(PROJECT_ROOT), '.venv', 'Scripts', 'python.exe')
# 用于比较的规范化路径（Windows 下 normcase 会转小写并统一斜杠）
_EXPECTED_KEY = os.path.normcase(EXPECTED_PYTHON)

CORE_MODULES = ('numpy', 'bleak')
UI_MODULES = ('PyQt5', 'pyqtgraph')


def _missing(names: Iterable[str]) -> List[str]:
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:                     # 包括 ImportError 与依赖 DLL 缺失的 OSError
            missing.append(name)
    return missing


def _is_expected_interpreter() -> bool:
    try:
        return os.path.normcase(sys.executable) == _EXPECTED_KEY
    except Exception:
        return False


def check_dependencies(need_ui: bool = False) -> None:
    """缺依赖时打印中文指引并退出（退出码 2），不抛一堆 traceback。"""
    required = CORE_MODULES + (UI_MODULES if need_ui else ())
    missing = _missing(required)
    if not missing:
        return

    lines = [
        '',
        '=' * 68,
        '[启动失败] 缺少依赖：{}'.format('、'.join(missing)),
        '',
        '当前解释器：{}'.format(sys.executable),
        '应该用：    {}'.format(EXPECTED_PYTHON),
    ]
    if not _is_expected_interpreter():
        lines += [
            '',
            '原因：当前不是项目自带的虚拟环境。PyCharm 里新建的 Run Configuration',
            '      默认不会继承项目解释器，需要手动指定一次。',
        ]
    lines += [
        '',
        '修复方式（任选一种）：',
        '  1) PyCharm：Run → Edit Configurations → Python interpreter',
        '     选 {}'.format(EXPECTED_PYTHON),
        '  2) 命令行用绝对路径启动：',
        '     "{}" run_monitor.py'.format(EXPECTED_PYTHON),
        '  3) 如果是新机器，先装依赖：',
        '     "{}" -m pip install -r requirements.txt'.format(EXPECTED_PYTHON),
        '=' * 68,
        '',
    ]
    print('\n'.join(lines), file=sys.stderr)
    raise SystemExit(2)
