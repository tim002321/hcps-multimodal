# -*- coding: utf-8 -*-
"""眼动分析工具 —— 实时界面。

左：摄像头画面（人脸框、眼框、瞳孔圈、HUD）
右：眨眼与瞳孔指标
下：瞳孔直径曲线（近 20 秒）+ 基线

快捷键：空格 开始/停止，S 截图，Q 退出
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import deque

import cv2
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eyetool.capture import FfmpegCapture                    # noqa: E402
from eyetool.params import Params                            # noqa: E402
from eyetool.pipeline import EyePipeline, annotate, draw_hud   # noqa: E402

PREVIEW_W, PREVIEW_H = 960, 540
PLOT_WINDOW = 20.0

# 摄像头选择记在这里（在 out/ 下，不会进版本库）
PREFS_PATH = os.path.join(HERE, 'out', 'camera_prefs.json')
# 分辨率候选：4K 相机与普通 1080p 相机都覆盖到。注意 4K 只有新相机支持，
# 内置摄像头强行要 3840×2160 会让 ffmpeg 直接打不开（"Could not set video options"）。
SIZES = ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 720))


def load_prefs():
    """上次选的摄像头与分辨率：{'device': .., 'width': .., 'height': ..}。"""
    try:
        import json
        with open(PREFS_PATH, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:                                      # noqa: BLE001
        pass
    return {}


def save_prefs(device, width, height):
    try:
        import json
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, 'w', encoding='utf-8') as fh:
            json.dump({'device': device, 'width': int(width), 'height': int(height)},
                      fh, ensure_ascii=False, indent=2)
    except Exception:                                      # noqa: BLE001 - 记不住不影响采集
        pass


class ScanThread(QtCore.QThread):
    """后台列 DirectShow 设备名：ffmpeg -list_devices 要 0.3~1 秒，不能卡界面。"""

    done = QtCore.pyqtSignal(list)

    def run(self):
        names = []
        try:
            from eyetool.params import find_ffmpeg, list_devices
            names = list_devices(find_ffmpeg())
        except Exception:                                  # noqa: BLE001
            names = []
        self.done.emit(names)



class Worker(QtCore.QThread):
    updated = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str)
    status = QtCore.pyqtSignal(str)

    def __init__(self, params, out_dir='', parent=None):
        super().__init__(parent)
        self.params = params
        self.out_dir = out_dir or os.path.join(HERE, 'out', 'gui')
        self.running = True
        self.cap = None
        self.pipe = None
        self.rows = []
        self.pending = None

    def stop(self):
        self.running = False

    def run(self):
        # 建图要几秒：SCRFD 载入 CUDA、FaceMesh 初始化 tflite 图，
        # 加起来约 4 秒。之前是拿到第一帧才触发，界面会卡在"等待开始"，
        # 看起来像打不开摄像头，所以先把状态打出来，再拿一张合成帧预热。
        self.status.emit('正在初始化模型（约 4 秒）…')
        try:
            self.pipe = EyePipeline(self.params)
        except Exception as exc:                            # noqa: BLE001
            self.failed.emit('初始化失败：{}'.format(exc))
            return
        try:
            blank = np.zeros((480, 640, 3), np.uint8)
            self.pipe.process(blank, 0.0)          # 预热：把两个模型的图都建好
            self.pipe.detector.detect(blank)
        except Exception:                                  # noqa: BLE001
            pass
        self.status.emit('正在打开摄像头…')
        try:
            self.cap = FfmpegCapture(self.params.capture).start()
        except Exception as exc:                            # noqa: BLE001
            # 把完整堆栈写文件：对话框一行放不下，而且"expected string or
            # bytes-like object, got 'NoneType'" 这种话脱离堆栈根本判不出是哪一步
            import traceback
            tb = traceback.format_exc()
            path = ''
            try:
                os.makedirs(self.out_dir, exist_ok=True)
                path = os.path.join(self.out_dir, 'error_%s.log'
                                    % time.strftime('%Y%m%d_%H%M%S'))
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write('摄像头：%s  %dx%d @ %d\n\n%s' % (
                        self.params.capture.device, self.params.capture.width,
                        self.params.capture.height, self.params.capture.fps, tb))
            except Exception:                              # noqa: BLE001
                pass
            tail = str(exc).strip() or exc.__class__.__name__
            msg = '打不开摄像头：\n%s' % tail
            if 'already in use' in tb or 'in use' in tail:
                msg += '\n\n提示：摄像头被别的程序占着（常见是另一个眼动工具实例、相机 App、Teams/Zoom）。关掉再试。'
            if path:
                msg += '\n\n详细堆栈：%s' % path
            self.failed.emit(msg)
            return

        t0 = time.perf_counter()
        last_emit = 0.0
        n = 0
        t_first = None
        while self.running:
            frame, idx, dev_t = self.cap.read(0.5)
            if frame is None:
                if self.cap is not None and not self.cap.alive():
                    self.failed.emit('采集中断：{}'.format(self.cap.error or 'ffmpeg 已退出'))
                    break
                continue
            t_abs = dev_t if dev_t else (time.perf_counter() - t0)
            if t_first is None:
                t_first = t_abs
            t = t_abs - t_first          # 曲线和 CSV 都用相对时间
            res = self.pipe.process(frame, t)
            n += 1
            def _num(v, nd=2):
                return round(float(v), nd) if isinstance(v, float) and np.isfinite(v) else ''
            self.rows.append([
                idx, round(t, 4), int(res['face_ok']),
                max([v.get('eye_w', 0) for v in res['eyes'].values()] or [0]),
                _num(res['d_mean']), _num(res['baseline']), _num(res['delta_pct']),
                int(res['closed']), res['blinks'], _num(res['bpm'])])

            now = time.perf_counter()
            if now - last_emit < 1.0 / 30.0:
                continue
            last_emit = now
            small = annotate(frame, res, [], size=(PREVIEW_W, PREVIEW_H))
            small = draw_hud(small, [
                'frame %d   %.1f fps   %s' % (idx, n / max(1e-6, now - t0),
                                              self.pipe.detector.active_provider),
                'pupil %.1f px   base %.1f   delta %+.1f%%' % (
                    res['d_mean'], res['baseline'], res['delta_pct']),
                'blinks %d   bpm %.1f   closed %s' % (
                    res['blinks'], res['bpm'], 'Y' if res['closed'] else 'n'),
            ])
            self.updated.emit({'vis': small, 'res': res, 't': t,
                               'fps': n / max(1e-6, now - t0)})
        try:
            self.cap.stop()
        except Exception:                                   # noqa: BLE001
            pass


class Window(QtWidgets.QMainWindow):
    def __init__(self, params, out_dir):
        super().__init__()
        self.params = params
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.setWindowTitle('眼动分析工具 · 实时')
        self.resize(1320, 800)
        self.plot_t = deque(maxlen=1200)
        self.plot_d = deque(maxlen=1200)
        self.last_vis = None
        self.worker = None

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        top = QtWidgets.QHBoxLayout()
        root.addLayout(top, 3)

        self.view = QtWidgets.QLabel('等待开始…')
        self.view.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.view.setStyleSheet('background:#111; color:#888;')
        self.view.setAlignment(QtCore.Qt.AlignCenter)
        top.addWidget(self.view)

        panel = QtWidgets.QVBoxLayout()
        top.addLayout(panel)
        self.labels = {}
        for key, title in (('fps', '处理帧率'), ('face', '人脸'),
                           ('eye', '眼宽'), ('pupil', '瞳孔直径'),
                           ('base', '基线'), ('delta', '相对变化'),
                           ('blinks', '眨眼次数'), ('bpm', '眨眼频率'),
                           ('dur', '平均时长'), ('closed', '闭眼占比')):
            box = QtWidgets.QVBoxLayout()
            t = QtWidgets.QLabel(title)
            t.setStyleSheet('color:#666; font-size:12px;')
            v = QtWidgets.QLabel('--')
            v.setStyleSheet('color:#111; font-size:22px; font-weight:600;')
            box.addWidget(t)
            box.addWidget(v)
            panel.addLayout(box)
            self.labels[key] = v
        panel.addStretch(1)

        self.plot = pg.PlotWidget()
        self.plot.setBackground('w')
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel('left', '瞳孔直径 (mm)')
        self.plot.setLabel('bottom', '时间 (s)')
        self.plot.setYRange(0, 40)          # 首帧数据到了会按实测值自适应
        self._y_span = None
        self.curve = self.plot.plot([], [], pen=pg.mkPen('#185FA5', width=2))
        self.base_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('#BA7517', width=1,
                                                               style=QtCore.Qt.DashLine))
        self.plot.addItem(self.base_line)
        root.addWidget(self.plot, 2)

        bar = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton('开始')
        self.btn_shot = QtWidgets.QPushButton('截图')
        self.status = QtWidgets.QLabel('就绪')
        self.status.setStyleSheet('color:#666;')
        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_shot)
        bar.addWidget(self.status, 1)
        root.addLayout(bar)

        # 摄像头选择：边看画面边换设备，不用改代码/命令行参数。
        # 放在最下面一行，不影响上面预览与曲线的高度分配。
        cam = QtWidgets.QHBoxLayout()
        cam.addWidget(QtWidgets.QLabel('摄像头'))
        self.combo_device = QtWidgets.QComboBox()
        self.combo_device.setMinimumWidth(260)
        self.combo_device.setToolTip(
            '本机所有 USB/UVC 摄像头（DirectShow）。\n'
            '换设备后点「开始」生效；选择会记住，下次打开自动选回同一台。')
        self.combo_device.currentIndexChanged.connect(self.on_device_changed)
        cam.addWidget(self.combo_device, 1)

        self.btn_scan = QtWidgets.QPushButton('刷新')
        self.btn_scan.setFixedWidth(56)
        self.btn_scan.setToolTip('重新扫描摄像头（插拔后点这里）')
        self.btn_scan.clicked.connect(lambda: self.scan_devices(manual=True))
        cam.addWidget(self.btn_scan)

        cam.addWidget(QtWidgets.QLabel('  分辨率'))
        self.combo_size = QtWidgets.QComboBox()
        self.combo_size.setMinimumWidth(130)
        for w, h in SIZES:
            self.combo_size.addItem('%d×%d' % (w, h), (w, h))
        self.combo_size.currentIndexChanged.connect(self.on_size_changed)
        cam.addWidget(self.combo_size)
        root.addLayout(cam)

        self._devices = []
        self._scan_thread = None
        # 先按当前参数占位，扫描结果回来再对齐
        self.combo_device.blockSignals(True)
        self.combo_device.addItem(self.params.capture.device, self.params.capture.device)
        self.combo_device.blockSignals(False)
        self._sync_size_combo()
        QtCore.QTimer.singleShot(150, self.scan_devices)

        self.btn_start.clicked.connect(self.toggle)
        self.btn_shot.clicked.connect(self.snapshot)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick_labels)
        self.timer.start(200)

    # ---------------------------------------------------------- 摄像头选择
    def scan_devices(self, manual=False):
        """后台列一遍本机摄像头。"""
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        if manual:
            self.status.setText('正在扫描摄像头…')
        self.btn_scan.setEnabled(False)
        self._scan_thread = ScanThread(self)
        self._scan_thread.done.connect(self.on_devices)
        self._scan_thread.start()

    def on_devices(self, names):
        self.btn_scan.setEnabled(True)
        current = self.params.capture.device
        self._devices = list(names or [])
        if not self._devices:
            self.status.setText('没扫到摄像头：检查 USB / 是否被别的程序独占')
            return

        self.combo_device.blockSignals(True)
        self.combo_device.clear()
        for name in self._devices:
            self.combo_device.addItem(name, name)
        if current and current not in self._devices:
            # 参数里配的那台不在了：仍留在列表首位并标注，方便看出"为什么画面不对"
            self.combo_device.insertItem(0, '%s（未检测到）' % current, current)
        pick = self.combo_device.findData(current)
        self.combo_device.setCurrentIndex(pick if pick >= 0 else 0)
        self.combo_device.blockSignals(False)
        if pick < 0:
            self.status.setText('配置的「%s」不在线，已选 %s'
                                % (current, self._devices[0]))
        else:
            self.status.setText('摄像头 %d 台' % len(self._devices))

    def on_device_changed(self, _row):
        name = self.combo_device.currentData() or ''
        if not name:
            return
        self.params.capture.device = str(name)
        # 4K 相机之外多半只支持到 1080p；当前若还挂在 4K 上直接改成 1080p，
        # 否则 ffmpeg 起流会失败（报 Could not set video options）。
        if (self.params.capture.width >= 3000
                and not any(k in str(name) for k in ('4K', '4k', '8K'))):
            self._set_size(1920, 1080)
            if self.status.text().startswith('摄像头'):
                self.status.setText('%s 已切到 1080p（4K 多半不支持）' % name)
        save_prefs(name, self.params.capture.width, self.params.capture.height)
        self._schedule_restart()

    def on_size_changed(self, _row):
        data = self.combo_size.currentData()
        if not data:
            return
        self.params.capture.width, self.params.capture.height = int(data[0]), int(data[1])
        save_prefs(self.params.capture.device, *data)
        self._schedule_restart()

    def _sync_size_combo(self):
        """把下拉框对齐到当前参数（含不在候选表里的自定义分辨率）。"""
        want = (int(self.params.capture.width), int(self.params.capture.height))
        combo = self.combo_size
        combo.blockSignals(True)
        if combo.findData(want) < 0:
            combo.insertItem(0, '%d×%d' % want, want)
        combo.setCurrentIndex(combo.findData(want))
        combo.blockSignals(False)

    def _set_size(self, width, height):
        self.params.capture.width = int(width)
        self.params.capture.height = int(height)
        self._sync_size_combo()

    def _set_camera_controls(self, enabled):
        """扫描按钮状态。设备/分辨率下拉**不禁用**：采集时换一台也能用，
        只是在内部自动停流再重开（见 _schedule_restart）——之前锁死下拉，
        用户会以为"选了也改不了"。"""
        self.btn_scan.setEnabled(enabled)

    def _schedule_restart(self):
        """换设备/分辨率后自动重开流。600ms 防抖，避免上下键选时连起重启。"""
        if self.worker is None:
            return
        timer = getattr(self, '_restart_timer', None)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._restart_worker)
            self._restart_timer = timer
        timer.start(600)

    def _restart_worker(self):
        if self.worker is None:
            return
        self.status.setText('切换设备，重新打开…')
        self._stop_worker()
        self._start_worker()

    def _start_worker(self):
        self.worker = Worker(self.params, self.out_dir)
        self.worker.updated.connect(self.on_update)
        self.worker.failed.connect(self.on_fail)
        self.worker.status.connect(self.status.setText)
        self.worker.start()
        self.btn_start.setText('停止')
        self.status.setText('运行中')

    def _stop_worker(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
            self.save_csv()
            self.worker = None
        self.btn_start.setText('开始')

    def toggle(self):
        if self.worker is None:
            self._start_worker()
        else:
            self._stop_worker()
            self.status.setText('已停止')

    def on_fail(self, msg):
        self.status.setText(msg)
        QtWidgets.QMessageBox.warning(self, '出错了', msg)
        self.worker = None
        self.btn_start.setText('开始')
        self._set_camera_controls(True)       # 打不开时把设备下拉放开，好换一台再试

    def on_update(self, payload):
        if self.status.text() != '运行中':
            self.status.setText('运行中')      # 第一帧到了，把"正在打开摄像头"换掉
        vis = payload['vis']
        res = payload['res']
        self.last_vis = vis
        rgb = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
        img = QtGui.QImage(rgb.data, rgb.shape[1], rgb.shape[0],
                           rgb.strides[0], QtGui.QImage.Format_RGB888).copy()
        self.view.setPixmap(QtGui.QPixmap.fromImage(img))

        t = payload['t']
        if np.isfinite(res['d_mean']):
            self.plot_t.append(t)
            self.plot_d.append(res['d_mean'])
        self.curve.setData(list(self.plot_t), list(self.plot_d))
        if np.isfinite(res['baseline']):
            self.base_line.setValue(res['baseline'])
        if self.plot_t:
            self.plot.setXRange(max(0, t - PLOT_WINDOW), t, padding=0)
            # 分辨率变了瞳孔像素也跟着变（1080p 约 20 px、4K 约 85 px），
            # Y 轴写死会把曲线切掉，所以按最近 5 秒的数据自适应，留 15% 余量
            recent = [d for tt, d in zip(self.plot_t, self.plot_d)
                      if tt >= t - 5.0]
            if recent:
                lo = min(recent)
                hi = max(recent)
                span = max(hi - lo, 6.0)
                lo -= span * 0.5
                hi += span * 0.5
                if (self._y_span is None
                        or abs(lo - self._y_span[0]) > span * 0.3
                        or abs(hi - self._y_span[1]) > span * 0.3):
                    self.plot.setYRange(max(0.0, lo), hi, padding=0)
                    self._y_span = (lo, hi)

        self.pending = payload

    def tick_labels(self):
        p = getattr(self, 'pending', None)
        if not p:
            return
        res = p['res']
        w = max([v.get('eye_w', 0) for v in res['eyes'].values()] or [0])
        self.labels['fps'].setText('%.0f' % p['fps'])
        self.labels['face'].setText('有' if res['face_ok'] else '无')
        self.labels['eye'].setText('%d px' % w)
        unit = ' mm' if res.get('unit') == 'mm' else ' px'
        self.labels['pupil'].setText(('%.2f' if unit == ' mm' else '%.1f') % res['d_mean']
                                     + unit if np.isfinite(res['d_mean']) else '--')
        self.labels['base'].setText(('%.2f' if unit == ' mm' else '%.1f') % res['baseline']
                                    + unit if np.isfinite(res['baseline']) else '--')
        self.labels['delta'].setText('%+.1f%%' % res['delta_pct']
                                     if np.isfinite(res['delta_pct']) else '--')
        self.labels['blinks'].setText(str(res['blinks']))
        self.labels['bpm'].setText('%.1f' % res['bpm'] if np.isfinite(res['bpm']) else '--')
        if self.worker and self.worker.pipe:
            d = self.worker.pipe.blink.mean_duration_ms()
            self.labels['dur'].setText('%.0f ms' % d if np.isfinite(d) else '--')
            self.labels['closed'].setText('%.1f%%' % (
                self.worker.pipe.metrics.closed_fraction() * 100))

    def snapshot(self):
        path = os.path.join(self.out_dir, 'shot_%s.png' % time.strftime('%H%M%S'))
        if self.last_vis is not None:
            cv2.imencode('.png', self.last_vis)[1].tofile(path)
        self.grab().save(path.replace('.png', '_ui.png'))
        self.status.setText('已保存 ' + os.path.basename(path))

    def save_csv(self):
        if not (self.worker and self.worker.rows):
            return
        path = os.path.join(self.out_dir, 'live_%s.csv' % time.strftime('%H%M%S'))
        with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(['frame', 't', 'face_ok', 'eye_w', 'd_mean',
                        'baseline', 'delta', 'closed', 'blinks', 'bpm'])
            w.writerows(self.worker.rows)
        self.status.setText('CSV: ' + os.path.basename(path))

    def closeEvent(self, ev):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
            self.save_csv()
        ev.accept()


def _acquire_single_instance():
    """Windows 命名互斥量做单实例保护，绕不过去也不会残留。

    摄像头的 DirectShow 是独占的：上一次的窗口如果卡在错误对话框上没退出，
    它仍然占着设备，此时再按 F5 只会得到"打不开摄像头"，而真正的用户在
    界面上看不出是谁占的。所以第二次启动直接拒绝，并说清楚怎么处理。
    """
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, True, 'Global\\EyeToolSingleInstance')
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:                           # noqa: BLE001
        return True                             # 拿不到就放行，别挡住正常使用


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default=None)
    ap.add_argument('--width', type=int, default=0)
    ap.add_argument('--height', type=int, default=0)
    ap.add_argument('--fps', type=int, default=0)
    ap.add_argument('--out', default=os.path.join(HERE, 'out', 'gui'))
    ap.add_argument('--provider', default='auto', choices=('auto', 'cuda', 'cpu'))
    ap.add_argument('--autostart', action='store_true')
    ap.add_argument('--shot', type=float, default=0, help='N 秒后自动截图并退出（自检用）')
    ap.add_argument('--list-devices', action='store_true',
                    help='只列出本机摄像头然后退出（查"打不开"用）')
    args = ap.parse_args()

    if args.list_devices:
        from eyetool.params import find_ffmpeg, list_devices
        ff = find_ffmpeg()
        names = list_devices(ff)
        print('ffmpeg: %s' % ff)
        print('摄像头（DirectShow）：')
        for n in names:
            print('   - %s' % n)
        if not names:
            print('   （一个都没找到：检查 USB 是否插好、驱动是否装、是否被别的程序独占）')
        return

    params = Params()
    params.detect.provider = args.provider
    params = params.resolve_paths(HERE)

    # 优先级：命令行 > 上次在界面里选的 > 代码默认值。
    # （界面里换设备会写 out/camera_prefs.json，下次开就还是那台）
    prefs = load_prefs()
    if prefs:
        if prefs.get('device'):
            params.capture.device = str(prefs['device'])
        try:
            if prefs.get('width'):
                params.capture.width = int(prefs['width'])
            if prefs.get('height'):
                params.capture.height = int(prefs['height'])
        except (TypeError, ValueError):
            pass
    if args.device:
        params.capture.device = args.device
    if args.width:
        params.capture.width = args.width
    if args.height:
        params.capture.height = args.height
    if args.fps:
        params.capture.fps = args.fps

    # 报出解释器：在 VS Code 里最容易犯的错是用错 python（anaconda 而非项目 venv），
    # 表现是缺库、或某些库行为和预期不一样，而报错信息完全对不上
    print('[眼动] python: %s' % sys.executable)
    if 'shifeiyuantwo' not in sys.executable.replace('/', '\\').lower():
        print('[眼动] 注意：当前不是项目 venv 的解释器，建议用 '
              r'试飞院二期\shifeiyuantwo\Scripts\python.exe')
    print('[眼动] 摄像头「%s」 %dx%d @ %d fps，采集方式 %s' % (
        params.capture.device, params.capture.width, params.capture.height,
        params.capture.fps, params.capture.pipe))

    _guard = _acquire_single_instance()
    pg.setConfigOptions(antialias=True)
    app = QtWidgets.QApplication(sys.argv)
    if _guard is None:
        QtWidgets.QMessageBox.warning(
            None, '已经在运行',
            '眼动工具已经有一个实例在运行。\n\n'
            '摄像头是独占设备，同时开两个会打不开。\n'
            '请切换到已打开的那个窗口；如果看不到，'
            '在任务管理器里结束残留的 python.exe 再试。')
        return
    win = Window(params, args.out)
    win.show()
    if args.autostart or args.shot:
        QtCore.QTimer.singleShot(400, win.toggle)
    if args.shot:
        def _shot():
            win.snapshot()
            win.close()
            app.quit()
        QtCore.QTimer.singleShot(int(args.shot * 1000), _shot)
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
