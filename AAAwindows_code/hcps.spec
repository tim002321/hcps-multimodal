# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述：HCPS 采集程序（含运行时环境）。

用法（在工作区根目录执行）：
    shifeiyuantwo\\Scripts\\python.exe -m PyInstaller --noconfirm --clean ^
        --distpath dist --workpath build AAAwindows_code\\hcps.spec

设计要点：
  * onedir（不是 onefile）：torch 有 500+ MB，onefile 每次启动都要解包到临时目录，
    现场机器上要等半分钟才起得来；onedir 拷文件夹即用。
  * 数据文件一起打进去：config.ini 模板、models/（人脸 + 哈欠模型）、ui/assets、
    docs/、ppg_pipeline 源码（四通道融合按需 import）。
  * 可写的东西（data/、config.ini）由 core/paths.py 放到 exe 旁边，不写任何绝对路径。
  * _build_hooks/qt_plugin_hook.py 在 Qt 初始化前设置插件目录：中文目录下 Qt5 会把
    路径按 ANSI 转成 ?????，不设就会报 "no Qt platform plugin could be initialized"。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = os.path.abspath(SPECPATH)                 # AAAwindows_code/
ROOT = os.path.dirname(HERE)                     # 工作区根目录

datas = [
    (os.path.join(HERE, 'config.ini'), '.'),                 # 模板：首次运行复制到 exe 旁边
    (os.path.join(HERE, 'models'), 'models'),                # yolo-face + 哈欠 onnx
    (os.path.join(HERE, 'ui', 'assets'), os.path.join('ui', 'assets')),
    (os.path.join(HERE, 'docs'), 'docs'),
    (os.path.join(ROOT, 'ppg_pipeline'), 'ppg_pipeline'),    # 四通道融合管线（源码）
]

binaries = []
hiddenimports = [
    'scipy', 'scipy.signal', 'scipy.spatial', 'scipy.spatial.distance',
    'scipy.special', 'scipy.optimize',
    'pylsl',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'onnxruntime', 'onnxruntime.capi._pybind_state',
    'bleak',
    'analysis.ppg_fusion',
    'vision.pose_gate',
    'core.paths',
    'torchvision', 'torchvision.ops', 'torchvision.extension',
]

# bleak 在 Windows 上全靠 winrt（pywinrt），PyInstaller 的静态分析看不到
for name in collect_submodules('winrt'):
    hiddenimports.append(name)

# ultralytics / onnxruntime 的模型加载器是动态 import，整包收进来更稳
for module in ('ultralytics', 'onnxruntime', 'torchvision'):
    try:
        c_datas, c_binaries, c_hidden = collect_all(module)
        datas += c_datas
        binaries += c_binaries
        hiddenimports += c_hidden
    except Exception as exc:                                  # noqa: BLE001
        print('[spec] collect_all({}) 失败：{}'.format(module, exc))

excludes = [
    'tkinter', 'test',
    'matplotlib.backends.backend_tkagg', 'matplotlib.backends._tkagg',
    # 界面只用 QtWidgets + pyqtgraph，这些不会被 import，剔掉能省几十 MB
    'PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebEngineCore', 'PyQt5.QtWebEngine',
    'PyQt5.QtQml', 'PyQt5.QtQuick', 'PyQt5.QtQuickWidgets',
    'PyQt5.QtMultimedia', 'PyQt5.QtMultimediaWidgets',
    'PyQt5.QtDesigner', 'PyQt5.QtHelp', 'PyQt5.QtLocation',
    'PyQt5.QtNfc', 'PyQt5.QtPositioning', 'PyQt5.QtSensors',
    'PyQt5.QtSerialPort', 'PyQt5.QtSql', 'PyQt5.QtTest',
    'PyQt5.QtWebChannel', 'PyQt5.QtWebSockets', 'PyQt5.QtBluetooth',
    'PyQt5.Qt3DCore', 'PyQt5.Qt3DRender', 'PyQt5.Qt3DInput',
    'PyQt5.Qt3DLogic', 'PyQt5.Qt3DAnimation', 'PyQt5.Qt3DExtras',
    'IPython', 'jupyter', 'pytest',
]

a = Analysis(
    [os.path.join(HERE, 'run_monitor.py')],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(HERE, '_build_hooks', 'qt_plugin_hook.py')],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HCPS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # torch / Qt 的 dll 用 upx 压过容易加载失败
    console=False,                 # 窗口程序；排查时可临时改 True 重打
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='HCPS',
)
