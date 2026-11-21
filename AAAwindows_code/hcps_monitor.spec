# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir + 无控制台 + 带 CUDA torch。

构建方式（用脚本，别直接敲 pyinstaller）：
    python build_exe.py

产物结构（dist/HcpsMonitor/）：
    HcpsMonitor.exe      主程序，双击运行
    config.ini           可编辑配置（由 build_exe.py 从源码树复制过来）
    data/                采集数据，首次运行自动创建
    logs/                运行日志（无控制台模式下的 stdout/stderr 都写这里）
    _internal/           依赖、模型权重、Qt 插件（勿动）

注意：这里**不打包** data/ 与 ui_shots/ —— 前者 1.8GB 的历史采集数据，
后者是界面截图，都不属于软件本体。
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = os.path.abspath(SPECPATH)          # spec 所在目录 = 项目根

# bleak 3.x 走 WinRT 后端，依赖 winrt 的 9 个 _winrt_*.pyd 扩展；
# pyinstaller-hooks-contrib 里没有 winrt 的钩子，不显式收集的话蓝牙扫描会报
# "No module named 'winrt.windows.devices.bluetooth'"。
winrt_datas, winrt_binaries, winrt_hidden = collect_all('winrt')

# ultralytics 的模型构造走动态属性访问，只靠 hook 收的 cfg/*.yaml 不够，
# 漏收会在 YOLO(...) 那一刻抛 AttributeError。
ultralytics_hidden = collect_submodules('ultralytics')

datas = [
    # 包内模板：exe 同级没有 config.ini 时，程序会复制这份出来（见 core/config.py）
    (os.path.join(HERE, 'config.ini'), '.'),
    # 15.6MB 权重：yawn_model_80.onnx + yolov8n-face.pt
    (os.path.join(HERE, 'models'), 'models'),
    (os.path.join(HERE, 'ui', 'assets'), os.path.join('ui', 'assets')),
] + winrt_datas

hiddenimports = winrt_hidden + ultralytics_hidden

# 只排除确定用不到的东西。scipy / matplotlib / pandas 不动 —— ultralytics 会间接用到。
#
# onnxruntime 必须排除：PyInstaller 收集它时要在隔离子进程里 import，
# 而那个进程已经先加载了 torch 的 CUDA/MKL 原生库，两者撞在一起直接
# 0xC0000005（访问违例），构建当场失败。项目默认走 cv2.dnn 做张嘴分类
# （vision/mouth_state.py 的 auto 顺序是 cv2.dnn → onnxruntime），
# 排掉它不影响功能，还省 47MB。
#
# tensorflow / keras 是 ultralytics 的**可选导出后端**，只在
# nn/backends/tensorflow.py、utils/export/tensorflow.py 的函数内部 import，
# 我们只做推理不做导出。不排的话它们会被拖进来，白多约 1GB。
excludes = [
    'pylsl',            # 肌电，主程序从不 import
    'serial',           # 旧版串口手环，主程序从不 import
    'tkinter',          # 界面是 PyQt5
    'IPython',
    'jupyter',
    'notebook',
    'onnxruntime',
    'tensorflow',
    'tensorflow_estimator',
    'keras',
    'paddle',
    'paddlepaddle',
    'jax',
]

a = Analysis(
    [os.path.join(HERE, 'run_monitor.py')],
    pathex=[HERE],
    binaries=winrt_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HcpsMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压缩 torch / CUDA 的 DLL 极易导致加载失败（解压到内存时签名/对齐出问题）
    upx=False,
    # 无控制台窗口；stdout/stderr 由 run_monitor._setup_frozen_stdio() 接到 logs/app.log
    console=False,
    disable_windowed_traceback=False,      # 崩溃时弹窗显示 traceback，便于现场反馈
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
    name='HcpsMonitor',
)
