# -*- coding: utf-8 -*-
"""把界面六个页面离屏渲染成 PNG（没有显示器也能看效果、也能当回归网）。

用法：
    python tools/screenshot_ui.py                        # 输出到 实验结果/_ui_shots
    python tools/screenshot_ui.py --out D:\\shots --seconds 4

做法：用 Qt 的 offscreen 平台插件起界面 → 自动开一个模拟源会话 → 灌几秒数据 →
逐页 widget.grab() 存图。分析页和表情页会真的跑一遍 A2，所以图里是有内容的。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')     # 必须在建 QApplication 之前

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt5.QtWidgets import QApplication                                # noqa: E402

from core.A3_ui import MainWindow, create_app                           # noqa: E402
from core.A3_ui.main_window import load_yaml                            # noqa: E402
from tools.synth_session import build_session                           # noqa: E402

PAGE_NAMES = ['overview', 'collect', 'grip', 'analyze', 'face', 'settings']


class FakeCamera:
    """截图自检用的假摄像头：把示例人脸图当帧喂进去（这台机器没有可用摄像头）。"""

    def __init__(self, index=0, backend=0, frame_path: str = ''):
        import cv2
        import numpy as np

        self._opened = True
        self._frames = 0
        image = cv2.imdecode(np.fromfile(frame_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        self._image = cv2.resize(image, (640, 480))

    def isOpened(self):                               # noqa: N802
        return self._opened

    def set(self, prop, value):                       # noqa: N802
        return True

    def get(self, prop):                              # noqa: N802
        return 640

    def read(self):                                   # noqa: N802
        self._frames += 1
        return True, self._image.copy()

    def release(self):                                # noqa: N802
        self._opened = False


def pump(app: QApplication, seconds: float) -> None:
    """跑事件循环 seconds 秒（让采集数据进来、图表画出来）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    parser = argparse.ArgumentParser(description='界面离屏截图')
    parser.add_argument('--out', default=str(ROOT / '实验结果' / '_ui_shots'))
    parser.add_argument('--seconds', type=float, default=4.0, help='灌多久模拟数据')
    parser.add_argument('--width', type=int, default=1520)
    parser.add_argument('--height', type=int, default=960)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(sys.argv)                       # 里面会注册系统字体，否则离屏渲染一个字都不显示
    window = MainWindow(load_yaml(str(ROOT / 'config' / 'default.yaml')), use_sim=True)
    window.resize(args.width, args.height)
    window.show()
    pump(app, 0.5)

    # 开一个模拟源会话，让曲线有数据
    window.pages[1].user_edit.setText('smoke')
    window.pages[1].task_edit.setText('uishot')
    window.pages[1]._start(fresh=False)
    pump(app, args.seconds)

    # 分析页用一条 120 秒的合成会话（实时那几秒太短，切不出特征窗口）
    analyze_db = build_session(seconds=120, task='uishot_analyze', fresh=True)['db_path']

    saved = []
    for index, name in enumerate(PAGE_NAMES):
        window.switch_page(index)
        if name == 'analyze':                       # 分析页：真的跑一遍 A2
            window.pages[index].db_edit.setText(analyze_db)
            window.pages[index]._run()
        if name == 'face':                          # 表情页：用示例人脸图跑一遍
            window.pages[index]._use_sample()
            # 识别在子进程里跑，第一次要等 TensorFlow 起来
            deadline = time.time() + 40
            while time.time() < deadline and window.pages[index].result_tile.value.text() == '识别中':
                app.processEvents()
                time.sleep(0.05)
            # 再把"实时监测"面板点亮（这台机器没有摄像头，用假摄像头喂示例图）
            sample = str(ROOT / 'models' / 'test_photo2.png')
            window.start_camera({'index': 0, 'fps': 4,
                                 'capture_factory': lambda i, b: FakeCamera(i, b, sample)})
            deadline = time.time() + 25
            while time.time() < deadline and window.pages[index]._live_frames < 2:
                app.processEvents()
                time.sleep(0.05)
            pump(app, 2.5)
            window.stop_camera()
        pump(app, 0.6)
        path = out_dir / f'{index + 1}_{name}.png'
        window.grab().save(str(path))
        saved.append(path)
        print(f'已保存 {path}')

    window.close()
    print(f'\n共 {len(saved)} 张，目录：{out_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
