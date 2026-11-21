# -*- mode: python ; coding: utf-8 -*-
"""FaceTool 打包配置（onedir）。

为什么要 onedir 而不是 onefile：torch 有 2 GB 级 DLL，onefile 每次启动都要解压，
首帧要等几十秒。onedir 启动是秒级。

模型文件放在 _internal/models，同时程序也认 exe 同级 models/ 优先——
用户想换权重直接丢到 exe 旁边即可，不必重新打包。
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPEC))                 # face_tool/
PROJECT = os.path.dirname(ROOT)                               # PyCharmMiscProject
MODELS = os.path.join(PROJECT, 'AAAwindows_code', 'models')

datas = []
binaries = []
hiddenimports = []

# torch / ultralytics 大量使用动态导入，静态分析看不全，整包收集
for package in ('torch', 'torchvision', 'ultralytics'):
    pkg_datas, pkg_bins, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_bins
    hiddenimports += pkg_hidden

hiddenimports += collect_submodules('ultralytics')
hiddenimports += ['dlib']
hiddenimports += [
    'PIL.Image', 'PIL.ImageDraw', 'yaml', 'requests', 'psutil',
    'torch.serialization', 'torch.nn.modules.container',
]

# 权重与 68 点模型：只带必需的两个
for name in ('yolov8n-face.pt', 'shape_predictor_68_face_landmarks.dat'):
    path = os.path.join(MODELS, name)
    if os.path.exists(path):
        datas.append((path, 'models'))

excludes = [
    # 与本事无关却被传递依赖拉进来的大件
    'tensorflow', 'keras', 'tensorboard', 'tensorboard_data_server',
    'jax', 'jaxlib', 'polars', 'pyarrow', 'onnx', 'onnxruntime',
    'matplotlib', 'pandas', 'scipy', 'sklearn', 'seaborn',
    'PyQt5', 'PySide2', 'PySide6', 'notebook', 'IPython', 'jupyter',
    'pytest', 'sphinx', 'torchaudio',
    'ultralytics.models.sam', 'ultralytics.models.nas', 'ultralytics.models.rtdetr',
]

# 构建模式：gui=正式窗口版，dbg=带控制台调试版，cli=命令行版（验证用）
MODE = os.environ.get('FT_MODE', 'gui')
ENTRY = os.path.join(ROOT, 'run_cli.py' if MODE == 'cli' else 'run_gui.py')
APP_NAME = {'gui': 'FaceTool', 'dbg': 'FaceToolDbg', 'cli': 'FaceToolCli'}.get(MODE, 'FaceTool')

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ---- 裁剪 ----
# 只砍运行时用不到的东西：头文件、测试、静态库。CUDA 库一概保留 —— 砍掉任何
# 一个 cuda dll，import torch 就会失败，auto 会静默降级到 dlib，数据跟着变。
DATA_SKIP = ('torch/include/', 'torch/test/', 'torch/testing/', '/tests/')


def _keep_data(dest: str) -> bool:
    low = dest.replace('\\', '/').lower()
    if any(low.endswith(sfx) for sfx in ('.h', '.hpp', '.lib', '.a', '.pdb')):
        return False
    return not any(mark in low for mark in DATA_SKIP)


a.datas = [item for item in a.datas if _keep_data(item[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=MODE != 'gui',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
