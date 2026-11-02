#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载打哈欠检测要用的模型权重。

    python tools/vision/fetch_models.py                # 下必需的两个（约 16 MB）
    python tools/vision/fetch_models.py --with-onnx    # 再加 CPU 降级用的 face onnx（12 MB）
    python tools/vision/fetch_models.py --force        # 重新下载
    python tools/vision/fetch_models.py --check        # 只校验不下载

权重来源与许可证：

| 文件 | 来源 | 字节数 | 许可证 |
|---|---|---|---|
| yolov8n-face.pt | akanametov/yolo-face v1.0.0（WIDERFace 训练） | 6,389,512 | GPL-3.0 |
| yawn_model_80.onnx | iglaweb/HippoYD（YawDD 训练） | 9,815,081 | Apache-2.0 |
| yolov8n-face.onnx | 同上（可选，无 GPU 时的降级后端） | 12,715,424 | GPL-3.0 |

权重不进版本库（仓库根 .gitignore 的 models/ 规则），换机器重跑本脚本即可。

网络说明（本机实测 2026-10-30）：
  * `github.com` release 资产可用，但走 302 到 objects.githubusercontent.com，约 40 KB/s
    —— 用 curl 带 `-C -` 断点续传，中断重跑会接着下。
  * `raw.githubusercontent.com` 本机 SSL 握手超时；`api.github.com` 对 >300KB 的响应会
    中途断流（schannel: server closed abruptly）。
  * **最终走 jsDelivr CDN（`cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>`）**，
    实测 315 KB/s、31 秒下完，作为首选源；上面几条留作备选。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from typing import List, Tuple

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'models')

GITHUB_UA = {'User-Agent': 'hcps-fetch-models/1.0'}

# yawn_model_80.onnx 在 HippoYD 仓库里的 blob sha（API 通道用）
HIPPO_YAWN_SHA = 'a3804b3e7d34de4994da8ce3b036787db522b3ac'

# 文件名 -> {sources: [(方式, 地址)], size, sha256, required}
# 方式：'curl' 直接下；'github_blob' 走 api.github.com 的 base64 通道
MODELS = {
    'yolov8n-face.pt': {
        'sources': [
            ('curl', 'https://cdn.jsdelivr.net/gh/akanametov/yolo-face@main/'
                     'yolov8n-face.pt'),
            ('curl', 'https://github.com/akanametov/yolo-face/releases/download/'
                     '1.0.0/yolov8n-face.pt'),
        ],
        'size': 6389512,
        'sha256': 'd545bf1add5aa736a4febac4f4f9245a6d596cd0fe70d5d57989fe0cb9e626ca',
        'required': True,
    },
    'yawn_model_80.onnx': {
        'sources': [
            ('curl', 'https://cdn.jsdelivr.net/gh/iglaweb/HippoYD@master/'
                     'out_epoch_80_full/yawn_model_80.onnx'),
            ('github_blob', 'https://api.github.com/repos/iglaweb/HippoYD/git/blobs/'
                            + HIPPO_YAWN_SHA),
        ],
        'size': 9815081,
        'sha256': '7e66017cee679103b2ce7334a68acf6132bfb55c05136c1330dcfe99e028be55',
        'required': True,
    },
    'yolov8n-face.onnx': {
        'sources': [
            ('curl', 'https://github.com/akanametov/yolo-face/releases/download/'
                     '1.0.0/yolov8n-face.onnx'),
        ],
        'size': 12715424,
        'sha256': '',
        'required': False,                    # 只在没有 GPU 时才需要
    },
}


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def human(nbytes: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if nbytes < 1024 or unit == 'GB':
            return '{:.1f} {}'.format(nbytes, unit)
        nbytes /= 1024
    return '{:.1f} GB'.format(nbytes)


def have_curl() -> bool:
    return shutil.which('curl') is not None


def fetch_curl(url: str, target: str, max_time: int = 1800) -> None:
    """curl 下载，`-C -` 断点续传（中断后重跑会接着下）。"""
    if not have_curl():
        raise RuntimeError('本机没有 curl，无法下载（Windows 10+ 自带 curl.exe）')
    tmp = target + '.part'
    cmd = ['curl', '-L', '--fail', '--retry', '3', '--retry-delay', '2',
           '-C', '-', '--max-time', str(max_time), '-o', tmp, url]
    print('  curl 下载中（支持断点续传，慢网约几分钟）…')
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError('curl 退出码 {}（文件可能只下了一部分，重跑本脚本会继续）'
                           .format(result.returncode))
    os.replace(tmp, target)


def download(name: str, entry: dict) -> str:
    """按 sources 顺序逐个尝试。"""
    target = os.path.join(MODELS_DIR, name)
    print('[下载] {}'.format(name))
    errors: List[str] = []
    for kind, url in entry['sources']:
        print('  源 {}：{}'.format(kind, url))
        try:
            if kind == 'curl':
                fetch_curl(url, target)
            else:
                fetch_github_blob(url, target)
            return target
        except Exception as exc:                          # noqa: BLE001 - 换下一个源
            print('    失败：{}'.format(exc))
            errors.append('{} -> {}'.format(kind, exc))
    raise RuntimeError('所有源都失败：\n  ' + '\n  '.join(errors))


def fetch_github_blob(url: str, target: str) -> None:
    """走 GitHub API 取 base64 内容（raw 域名不可达时的通道）。"""
    request = urllib.request.Request(url, headers={**GITHUB_UA,
                                                   'Accept': 'application/vnd.github+json'})
    print('  GitHub API blob 通道下载中…')
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read())
    if payload.get('encoding') != 'base64':
        raise RuntimeError('blob API 返回编码异常：{}'.format(payload.get('encoding')))
    raw = base64.b64decode(payload['content'])
    tmp = target + '.part'
    with open(tmp, 'wb') as handle:
        handle.write(raw)
    os.replace(tmp, target)
    print('  收到 {}'.format(human(len(raw))))


def verify(path: str, entry: dict) -> bool:
    size = os.path.getsize(path)
    ok = True
    if entry['size'] is not None and size != entry['size']:
        print('  [警告] 大小不符：期望 {} 字节，实际 {} 字节（可能还没下完）'
              .format(entry['size'], size))
        ok = False
    actual = sha256_of(path)
    if entry['sha256']:
        if actual.lower() != entry['sha256'].lower():
            print('  [错误] SHA256 不符：期望 {}'.format(entry['sha256']))
            ok = False
    if ok:
        print('  [OK] {}  SHA256={}'.format(human(size), actual))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description='下载打哈欠检测模型权重')
    parser.add_argument('--force', action='store_true', help='已存在也重新下载')
    parser.add_argument('--check', action='store_true', help='只校验不下载')
    parser.add_argument('--with-onnx', action='store_true',
                        help='同时下载 yolov8n-face.onnx（无 GPU 时的降级后端用）')
    args = parser.parse_args()

    print('模型目录：{}'.format(MODELS_DIR))
    os.makedirs(MODELS_DIR, exist_ok=True)
    print()

    failed = []
    for name, entry in MODELS.items():
        needed = entry['required'] or args.with_onnx
        if not needed:
            print('[跳过] {}（可选，需要时加 --with-onnx）'.format(name))
            continue

        target = os.path.join(MODELS_DIR, name)
        if os.path.exists(target) and not args.force:
            print('[已存在] {}'.format(name))
            if not verify(target, entry):
                failed.append(name)
            print()
            continue

        if args.check:
            print('[缺失] {}（--check 不下载）'.format(name))
            failed.append(name)
            continue

        try:
            download(name, entry)
        except Exception as exc:                          # noqa: BLE001 - 给用户看得懂的提示
            print('  [失败] {}'.format(exc))
            print('  提示：重跑本脚本会从断点继续；也可手动把文件放到 {}'.format(MODELS_DIR))
            failed.append(name)
            print()
            continue

        if not verify(target, entry):
            failed.append(name)
        print()

    print()
    if failed:
        print('[未完成] {}：{}'.format(len(failed), '、'.join(failed)))
        return 1
    print('[完成] 模型已就位：{}'.format(MODELS_DIR))
    for name in sorted(os.listdir(MODELS_DIR)):
        if name.endswith(('.pt', '.onnx')):
            print('  {}  {}'.format(human(os.path.getsize(os.path.join(MODELS_DIR, name))), name))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
