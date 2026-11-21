#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包成可迁移的 Windows 软件（PyInstaller onedir）。

    python build_exe.py                # 完整构建（约 10~20 分钟，产物约 6GB）
    python build_exe.py --skip-build   # 跳过构建，只重做产物后处理（调试用）

产物：dist/HcpsMonitor/
    HcpsMonitor.exe   双击运行，不需要装 Python
    config.ini        可编辑配置（构建后从源码树复制，用户可改设备名/参数）
    data/             采集数据目录
    logs/             app.log，出问题让现场发这个回来
    _internal/        依赖 + 模型权重（勿动）

为什么要后处理：PyInstaller 的 datas 会把 config.ini 打进 `_internal/`，
但用户要改的配置必须在 exe 同级。所以 exe 同级这份由本脚本复制，
`_internal/` 那份留作模板（用户删了能自动恢复，见 core/config.py 的 Config.load）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, 'hcps_monitor.spec')
DIST_DIR = os.path.join(HERE, 'dist', 'HcpsMonitor')
EXE_NAME = 'HcpsMonitor.exe'

USAGE_TEXT = """蓝牙生理数据采集分析软件 —— 使用说明
================================================

一、启动
    双击 HcpsMonitor.exe 即可，不需要安装 Python，也不需要联网。

二、目录说明
    config.ini     配置文件。设备名、扫描参数、字号倍率都在这里改，
                   改完重启程序生效。删掉也没关系，程序会自动生成一份默认的。
    data\\          采集数据。每个人的数据是一个以姓名命名的文件夹，
                   里面每个任务一个 .db 文件。备份/迁移直接拷这个目录。
    logs\\app.log   运行日志。出问题请把这个文件发回来。
    _internal\\     程序依赖和模型权重，请勿修改或删除。

三、首次使用
    1. 确认心电贴/手环已开机，在 config.ini 里填好设备名（或用登录界面的设备名）；
    2. 双击启动 → 填写人员姓名、任务名 → 选择测量工具 → 开始；
    3. 采集完成后进入「回放与选段」，选中区间即可回放观看。

四、注意
    1. 本程序带有 GPU 推理组件（约 5GB），请保证磁盘剩余空间充足；
    2. 首次启动会比之后慢一些（要加载 torch 与模型）；
    3. 部分杀毒软件可能对 PyInstaller 生成的 exe 报警，属误报，
       加信任即可；
    4. 换机器使用时，把整个 HcpsMonitor 文件夹整体拷贝过去，
       不要只拷 exe。
"""


def check_pyinstaller() -> bool:
    try:
        import PyInstaller            # noqa: F401
        return True
    except ImportError:
        return False


def build() -> int:
    """调用 PyInstaller 构建。返回进程退出码。"""
    cmd = [sys.executable, '-m', 'PyInstaller', SPEC, '--noconfirm', '--clean',
           '--distpath', os.path.join(HERE, 'dist'),
           '--workpath', os.path.join(HERE, 'build')]
    print('[打包] ' + ' '.join(cmd), flush=True)
    started = time.time()
    result = subprocess.run(cmd, cwd=HERE)
    print('[打包] 用时 {:.1f} 分钟，退出码 {}'.format(
        (time.time() - started) / 60.0, result.returncode), flush=True)
    return result.returncode


def postprocess() -> None:
    """把用户要碰的东西放到 exe 同级。"""
    exe_path = os.path.join(DIST_DIR, EXE_NAME)
    if not os.path.exists(exe_path):
        raise SystemExit('[打包] 没找到 {}，构建可能失败了'.format(exe_path))

    # 1) config.ini 放到 exe 同级（_internal 里那份是模板）
    target_cfg = os.path.join(DIST_DIR, 'config.ini')
    source_cfg = os.path.join(HERE, 'config.ini')
    if not os.path.exists(target_cfg):
        shutil.copyfile(source_cfg, target_cfg)
        print('[打包] 已复制 config.ini 到 exe 同级', flush=True)

    # 2) data / logs 目录（空目录也要有，用户一眼知道数据在哪）
    for name in ('data', 'logs'):
        path = os.path.join(DIST_DIR, name)
        os.makedirs(path, exist_ok=True)
    print('[打包] 已创建 data\\ 与 logs\\ 目录', flush=True)

    # 3) 使用说明
    with open(os.path.join(DIST_DIR, '使用说明.txt'), 'w',
              encoding='utf-8-sig') as fp:      # 带 BOM，记事本打开不乱码
        fp.write(USAGE_TEXT)
    print('[打包] 已生成 使用说明.txt', flush=True)


def report() -> None:
    """打印产物清单与体积。"""
    total = 0
    for root, _dirs, files in os.walk(DIST_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    print()
    print('=' * 56)
    print('产物目录：{}'.format(DIST_DIR))
    print('总体积：  {:.2f} GB'.format(total / 1024 ** 3))
    print()
    print('顶层内容：')
    for name in sorted(os.listdir(DIST_DIR)):
        path = os.path.join(DIST_DIR, name)
        size = 0
        if os.path.isdir(path):
            for root, _d, files in os.walk(path):
                for item in files:
                    try:
                        size += os.path.getsize(os.path.join(root, item))
                    except OSError:
                        pass
        else:
            size = os.path.getsize(path)
        print('  {:24s} {:>10.1f} MB'.format(name, size / 1024 ** 2))
    print('=' * 56)
    print('下一步：双击 HcpsMonitor.exe 验证；日志在 logs\\app.log')


def main() -> int:
    parser = argparse.ArgumentParser(description='打包 AAAwindows_code 为可迁移软件')
    parser.add_argument('--skip-build', action='store_true',
                        help='跳过 PyInstaller，只重做产物后处理')
    args = parser.parse_args()

    if not args.skip_build:
        if not check_pyinstaller():
            print('[打包] 没装 PyInstaller，先安装：', flush=True)
            print('  {} -m pip install pyinstaller'.format(sys.executable), flush=True)
            return 2
        code = build()
        if code != 0:
            print('[打包] PyInstaller 失败（退出码 {}），请看上面的报错'.format(code),
                  flush=True)
            return code

    postprocess()
    report()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
