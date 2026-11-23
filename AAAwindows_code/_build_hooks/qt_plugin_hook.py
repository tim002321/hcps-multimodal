# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：在 Qt 初始化之前把插件目录设成绝对路径。

为什么必须这么做：程序如果放在含中文的目录（比如桌面\\试飞院二期\\HCPS），
PyQt5 的 Qt5 会把自身所在目录按本地 ANSI 代码页转一次，中文变成 '?????'，
QLibraryInfo.PluginsPath 因此指向一个不存在的目录，启动直接报

    This application failed to start because no Qt platform plugin could be initialized.

这里用 sys._MEIPASS（打包后的资源目录，Unicode 路径）拼出插件目录塞进环境变量，
Qt 就会用它而不是自己推导。
"""
import os
import sys

_base = getattr(sys, '_MEIPASS', None)
if _base:
    _plugins = os.path.join(_base, 'PyQt5', 'Qt5', 'plugins')
    if os.path.isdir(_plugins):
        os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = _plugins
        os.environ.setdefault('QT_PLUGIN_PATH', _plugins)
