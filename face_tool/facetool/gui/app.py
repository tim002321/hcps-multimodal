# -*- coding: utf-8 -*-
"""主窗口：选目录 → 扫描 → 设参数 → 开始。

线程规则：worker 只写 ProgressState（加锁）与日志队列；本文件是唯一碰 Tk 的地方，
所有控件更新都发生在 _tick 里（主线程）。
"""
from __future__ import annotations

import json
import os
import queue
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from .. import __version__
from ..output import OutputLayout
from ..params import RunParams, default_models_dir
from ..session import (discover_session, discover_sessions,
                       looks_like_session)
from ..paths import app_root, is_within, open_folder, short_path
from .widgets import DirPicker, LogPane, StatCards
from .worker import AnalyzerWorker, ProgressState

REFRESH_MS = 200
STATE_FILE = os.path.join(app_root(), '.gui_state.json')


class FaceToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.info = None
        self.entries = []
        self.layout: Optional[OutputLayout] = None
        self.worker: Optional[AnalyzerWorker] = None
        self.state = ProgressState()
        self.lock = __import__('threading').Lock()
        self.log_q: 'queue.Queue[str]' = queue.Queue()
        self.done_q: 'queue.Queue[Dict[str, Any]]' = queue.Queue()
        self._build()
        self._load_state()
        self._tick()

    # ------------------------------------------------------------------ 界面
    def _build(self) -> None:
        self.root.title('面部 ROI 处理工具 v{}'.format(__version__))
        style = ttk.Style()
        try:
            style.theme_use('vista')
        except tk.TclError:
            pass

        outer = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        outer.pack(fill='both', expand=True)

        box = ttk.LabelFrame(outer, text='输入', padding=(10, 6))
        box.pack(fill='x')
        box.columnconfigure(0, weight=1)
        self.pick_in = DirPicker(box, '会话目录', on_change=self._on_input_changed)
        self.pick_in.grid(row=0, column=0, sticky='ew')
        ttk.Button(box, text='扫描', width=8, command=self._scan).grid(row=0, column=1, padx=(6, 0))
        self.info_label = ttk.Label(box, text='未选择目录', foreground='#666666')
        self.info_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

        out_box = ttk.LabelFrame(outer, text='输出', padding=(10, 6))
        out_box.pack(fill='x', pady=(8, 0))
        out_box.columnconfigure(0, weight=1)
        self.pick_out = DirPicker(out_box, '输出根目录', on_change=self._on_output_changed)
        self.pick_out.grid(row=0, column=0, sticky='ew')
        self.flat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(out_box, text='直接写入此目录（不套会话名）',
                        variable=self.flat_var,
                        command=self._on_output_changed).grid(row=0, column=1, padx=(8, 0))
        self.out_label = ttk.Label(out_box, text='—', foreground='#666666')
        self.out_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

        self._build_params(outer)
        self._build_actions(outer)

        log_box = ttk.LabelFrame(outer, text='日志', padding=(6, 4))
        log_box.pack(fill='both', expand=True, pady=(8, 0))
        self.log = LogPane(log_box, height=9)
        self.log.pack(fill='both', expand=True)

        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 900)
        self.root.geometry('{}x{}'.format(width, max(self.root.winfo_reqheight(), 640)))
        self.root.minsize(880, 620)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build_params(self, parent) -> None:
        box = ttk.LabelFrame(parent, text='参数', padding=(10, 6))
        box.pack(fill='x', pady=(8, 0))
        for col in range(8):
            box.columnconfigure(col, weight=1 if col % 2 else 0)

        def label(text, row, col):
            ttk.Label(box, text=text).grid(row=row, column=col, sticky='w',
                                           padx=(0 if col == 0 else 12), pady=2)

        def spin(var, row, col, width=7, values=None):
            widget = ttk.Combobox(box, textvariable=var, width=width, values=values) \
                if values else ttk.Entry(box, textvariable=var, width=width)
            widget.grid(row=row, column=col, sticky='w', pady=2)
            return widget

        self.var_backend = tk.StringVar(value='auto')
        self.var_conf = tk.StringVar(value='0.35')
        self.var_pick = tk.StringVar(value='center')
        self.var_roi_mode = tk.StringVar(value='hybrid')
        self.var_eye_w = tk.StringVar(value='0.40')
        self.var_eye_h = tk.StringVar(value='0.26')
        self.var_mouth_w = tk.StringVar(value='1.8')
        self.var_mouth_h = tk.StringVar(value='3.6')
        self.var_scale = tk.StringVar(value='1')
        self.var_quality = tk.StringVar(value='95')
        self.var_eye_fmt = tk.StringVar(value='png')
        self.annotate_var = tk.BooleanVar(value=False)
        self.index_var = tk.BooleanVar(value=True)

        label('检测后端', 0, 0); spin(self.var_backend, 0, 1, 9,
                                     ['auto', 'torch', 'onnx', 'dlib'])
        label('置信度', 0, 2); spin(self.var_conf, 0, 3)
        label('选脸', 0, 4); spin(self.var_pick, 0, 5, 9, ['center', 'largest'])
        label('ROI 模式', 0, 6); spin(self.var_roi_mode, 0, 7, 12,
                                     ['hybrid', 'face_ratio', 'landmark_box'])

        label('眼宽比', 1, 0); spin(self.var_eye_w, 1, 1)
        label('眼高比', 1, 2); spin(self.var_eye_h, 1, 3)
        label('嘴宽比', 1, 4); spin(self.var_mouth_w, 1, 5)
        label('嘴高比', 1, 6); spin(self.var_mouth_h, 1, 7)

        label('放大', 2, 0); spin(self.var_scale, 2, 1, 7, ['1', '2', '3', '4'])
        label('JPEG 质量', 2, 2); spin(self.var_quality, 2, 3)
        label('眼部格式', 3, 0); spin(self.var_eye_fmt, 3, 1, 7, ['png', 'jpg'])
        ttk.Checkbutton(box, text='生成 68 点自检图（含人脸，勿外传）',
                        variable=self.annotate_var).grid(row=2, column=4, columnspan=3,
                                                         sticky='w', pady=2)
        ttk.Checkbutton(box, text='自检图带编号', variable=self.index_var) \
            .grid(row=2, column=7, sticky='w', pady=2)

    def _build_actions(self, parent) -> None:
        box = ttk.Frame(parent)
        box.pack(fill='x', pady=(8, 0))
        self.btn_start = ttk.Button(box, text='开始处理', command=self._start)
        self.btn_start.pack(side='left')
        self.btn_stop = ttk.Button(box, text='停止', command=self._stop, state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        ttk.Button(box, text='打开输出目录',
                   command=lambda: open_folder(self.layout.dir if self.layout else '')
                   ).pack(side='left', padx=6)
        self.progress = ttk.Progressbar(box, mode='determinate', maximum=100)
        self.progress.pack(side='left', fill='x', expand=True, padx=(12, 6))
        self.percent_label = ttk.Label(box, text='0%', width=6)
        self.percent_label.pack(side='left')

        self.cards = StatCards(parent, [
            ('sessions', '会话'), ('total', '总帧数'), ('done', '已处理'),
            ('face_ok', '成功'), ('no_face', '无人脸'), ('fps', '帧/秒'),
            ('eta', '剩余'),
        ])
        self.cards.pack(fill='x', pady=(6, 0))

    # ------------------------------------------------------------------ 状态
    def _load_state(self) -> None:
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            data = {}
        self.pick_in.set(data.get('input', ''))
        self.pick_out.set(data.get('output', ''))
        self.flat_var.set(bool(data.get('flat', False)))
        self.var_backend.set(data.get('backend', 'auto'))
        self.var_conf.set(str(data.get('conf', '0.35')))
        self.log.append('就绪。选一个会话目录，点「扫描」查看帧数。')

    def _save_state(self) -> None:
        data = {
            'input': self.pick_in.get(), 'output': self.pick_out.get(),
            'flat': bool(self.flat_var.get()), 'backend': self.var_backend.get(),
            'conf': self.var_conf.get(),
        }
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ------------------------------------------------------------------ 事件
    def _on_input_changed(self, _value: str = '') -> None:
        if not self.pick_out.get():
            self._suggest_output()

    def _on_output_changed(self, _value: str = '') -> None:
        self._refresh_output_label()

    def _suggest_output(self) -> None:
        folder = self.pick_in.get()
        if not folder:
            return
        self.pick_out.set(os.path.join(app_root(), 'output'))

    def _refresh_output_label(self) -> None:
        folder = self.pick_out.get()
        if not folder:
            self.out_label.configure(text='未选择输出目录')
            return
        if self.entries:
            self.out_label.configure(text='实际输出：{}（{} 个会话，按原目录结构镜像）'.format(
                short_path(folder, 70), len(self.entries)))
            return
        name = 'session'
        if self.info is not None:
            name = self.info.name
        target = folder if self.flat_var.get() else os.path.join(folder, name)
        self.out_label.configure(text='实际输出：{}'.format(short_path(target, 88)))

    def _scan(self) -> None:
        folder = self.pick_in.get()
        if not folder:
            messagebox.showinfo('扫描', '先选一个目录')
            return
        self.info = None
        self.entries = []

        # 选中的本身就是会话 → 单会话；否则当批量根目录递归找
        if not looks_like_session(folder):
            try:
                entries = discover_sessions(folder)
            except Exception as exc:                   # noqa: BLE001
                self.info_label.configure(text='扫描失败：{}'.format(exc))
                return
            if entries:
                self.entries = entries
                total = sum(e.est_frames for e in entries)
                text = '批量：{} 个会话，约 {} 帧'.format(len(entries), total)
                self.info_label.configure(text=text, foreground='#1a7f37')
                self.log.append(text)
                for e in entries[:8]:
                    self.log.append('  {}（约 {} 帧）'.format(e.rel, e.est_frames))
                if len(entries) > 8:
                    self.log.append('  … 还有 {} 个'.format(len(entries) - 8))
                self._refresh_output_label()
                if not self.pick_out.get():
                    self._suggest_output()
                return

        try:
            self.info = discover_session(folder)
        except Exception as exc:                       # noqa: BLE001
            self.info_label.configure(text='扫描失败：{}'.format(exc))
            return
        if self.info.problem:
            self.info_label.configure(text='扫描失败：{}'.format(self.info.problem))
            return
        text = self.info.describe()
        if self.info.probe is not None:
            text += '　｜　库：{}（{}）'.format(os.path.basename(self.info.db_path),
                                              self.info.probe.describe())
        self.info_label.configure(text=text, foreground='#1a7f37')
        self.log.append('扫描完成：{}'.format(text))
        self._refresh_output_label()
        if not self.pick_out.get():
            self._suggest_output()

    # ------------------------------------------------------------------ 运行
    def _params(self) -> Optional[RunParams]:
        def num(var, default):
            try:
                return float(var.get())
            except (TypeError, ValueError):
                return default

        params = RunParams(
            backend=self.var_backend.get(),
            conf=num(self.var_conf, 0.35),
            face_pick=self.var_pick.get(),
            roi_mode=self.var_roi_mode.get(),
            eye_w_ratio=num(self.var_eye_w, 0.40),
            eye_h_ratio=num(self.var_eye_h, 0.26),
            mouth_w_scale=num(self.var_mouth_w, 1.8),
            mouth_h_scale=num(self.var_mouth_h, 3.6),
            roi_scale=num(self.var_scale, 1.0),
            jpeg_quality=int(num(self.var_quality, 95)),
            eye_format=self.var_eye_fmt.get(),
            annotate=bool(self.annotate_var.get()),
            annotate_index=bool(self.index_var.get()),
        )
        problems = params.problems()
        if problems:
            messagebox.showinfo('参数有误', '；'.join(problems))
            return None
        return params

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.entries and (self.info is None or not self.info.frames):
            self._scan()
            if not self.entries and (self.info is None or not self.info.frames):
                messagebox.showinfo('开始处理', '没有可处理的帧')
                return
        params = self._params()
        if params is None:
            return
        folder = self.pick_out.get()
        if not folder:
            messagebox.showinfo('开始处理', '先选输出目录')
            return
        source = self.pick_in.get() if self.entries else (
            self.info.session_dir if self.info else '')
        if source and is_within(folder, source):
            messagebox.showwarning(
                '输出位置不合适',
                '输出目录不能放在输入目录里面：' + os.linesep + os.linesep
                + '  输入：' + source + os.linesep
                + '  输出：' + folder + os.linesep + os.linesep
                + '产物里的 eyes_left / mouth 会被当成新会话再处理一遍。'
                + os.linesep + '请换一个独立目录，例如 D:\\处理后。')
            return

        self.log.clear()
        self.state = ProgressState()

        if self.entries:
            self.layout = None
            self.log.append('批量开始：{} 个会话，约 {} 帧 → {}'.format(
                len(self.entries), sum(e.est_frames for e in self.entries), folder))
            self._save_state()
            self.worker = AnalyzerWorker(None, params, None, self.state,
                                         self.lock, self.log_q, self.done_q,
                                         entries=self.entries, out_root=folder)
        else:
            self.layout = OutputLayout(folder, self.info.name, params,
                                       flat=bool(self.flat_var.get()))
            self.log.append('开始：{} 帧 → {}'.format(self.info.total, self.layout.dir))
            self._save_state()
            self.worker = AnalyzerWorker(self.info, params, self.layout, self.state,
                                         self.lock, self.log_q, self.done_q)

        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self.progress.configure(value=0)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel()
            self.btn_stop.configure(state='disabled')

    # ------------------------------------------------------------------ 定时
    def _tick(self) -> None:
        try:
            self._drain_logs()
            self._refresh_progress()
            self._drain_done()
        finally:
            self.root.after(REFRESH_MS, self._tick)

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_q.get_nowait()
            except queue.Empty:
                return
            if line.startswith('__stage__'):
                self.root.title('面部 ROI 处理工具 v{} — {}'.format(
                    __version__, line[len('__stage__'):]))
            else:
                self.log.append(line)

    def _refresh_progress(self) -> None:
        with self.lock:
            snapshot = ProgressState(**vars(self.state))
        percent = snapshot.percent()
        if abs(self.progress['value'] - percent) > 0.01:
            self.progress.configure(value=percent)
        self.percent_label.configure(text='{:.0f}%'.format(percent))
        if snapshot.batch:
            self.cards.set('sessions', '{}/{}'.format(snapshot.session_done,
                                                      snapshot.sessions))
        else:
            self.cards.set('sessions', '1')
        self.cards.set('total', str(snapshot.total))
        self.cards.set('done', str(snapshot.done))
        self.cards.set('face_ok', str(snapshot.face_ok))
        self.cards.set('no_face', str(snapshot.no_face))
        self.cards.set('fps', '{:.1f}'.format(snapshot.fps))
        self.cards.set('eta', '{:.0f}s'.format(snapshot.eta_s) if snapshot.eta_s else '—')

    def _drain_done(self) -> None:
        try:
            result = self.done_q.get_nowait()
        except queue.Empty:
            return
        self.btn_start.configure(state='normal')
        self.btn_stop.configure(state='disabled')
        if result.get('ok'):
            stats = result.get('stats', {})
            if result.get('batch'):
                self.log.append('批量完成：{} / {} 个会话 ｜ 成功 {} 帧 ｜ 无人脸 {} ｜ '
                                '{:.1f} 秒'.format(
                                    stats.get('done_sessions', 0), stats.get('sessions', 0),
                                    stats.get('face_ok', 0), stats.get('no_face', 0),
                                    result.get('elapsed', 0)))
                self.log.append('汇总：{}'.format(
                    os.path.join(result.get('dir', ''), '_batch_summary.csv')))
            else:
                self.log.append('完成：成功 {} ｜ 无人脸 {} ｜ 跳过 {} ｜ {:.1f} 秒'.format(
                    stats.get('face_ok', 0), stats.get('no_face', 0),
                    stats.get('skipped', 0), result.get('elapsed', 0)))
                self.log.append('报告：{}'.format(
                    os.path.join(result.get('dir', ''), 'summary.txt')))
            self.root.title('面部 ROI 处理工具 v{} — 完成'.format(__version__))
        else:
            self.log.append('失败：{}'.format(result.get('error', '')))
            messagebox.showerror('处理失败', str(result.get('error', ''))[:400])

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno('正在处理', '任务还在跑，确定退出？'):
                return
            self.worker.cancel()
        self._save_state()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    FaceToolApp(root)
    root.mainloop()
    return 0
