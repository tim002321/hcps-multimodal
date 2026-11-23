# -*- coding: utf-8 -*-
"""眼动工具箱：把「实时入库 / 录制 CSV / 导入到库 / 回放测试」合成一个窗口。

四个功能原来分别是独立脚本，这里合成一个程序，共用一套「设备地址 / 端口 / 数据目录」，
避免开四个窗口、每个都填一遍；也避免两边同时连设备抢连接。

底层仍然是那几份脚本，没有另写一套逻辑：
    eye_live_gui    → SessionWatcher / DbSink / EyeLink（连接、找库、写库）
    import_eye_csv  → 导入流程（_run 原样调用，报告照旧）
    replay_eye_csv  → 回放分帧（load_frames）
    receive_tcp_5001→ CSV 表头与行格式（HEADER_JSON / rows_of）

用法：
    python debug_tools/eye/eye_toolkit_gui.py
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
BASE = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'AAAwindows_code')
if BASE not in sys.path:
    sys.path.insert(0, BASE)                     # receive_tcp_5001 在程序根目录

import eye_live_gui as live_mod                  # noqa: E402
import import_eye_csv as import_mod             # noqa: E402
import replay_eye_csv as replay_mod             # noqa: E402
import receive_tcp_5001 as rec_mod              # noqa: E402

TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
REFRESH_MS = 500


class RawLink(live_mod.EyeLink):
    """在 link 的基础上多给一份原始报文，录制 CSV 用。"""

    def __init__(self, host, port, on_frame, on_log, on_raw=None):
        super().__init__(host, port, on_frame, on_log)
        self.on_raw = on_raw

    def _handle(self, text: str) -> None:
        super()._handle(text)
        if self.on_raw is not None:
            self.on_raw(text)


class CsvWriter(threading.Thread):
    """按 receive_tcp_5001 的表头与格式写 CSV，保证两种方式产出同一个格式。

    写盘放在独立线程：接收线程只负责把原始报文丢进队列。118 Hz 下 CSV 才十几 KB/s，
    但盘慢（机械盘/网络盘）时同步写会拖住 recv，进而丢帧 —— 这里用队列把两者隔开，
    队列满时丢帧并计数，绝不阻塞接收。
    """

    def __init__(self, path: str, max_queue: int = 5000):
        super().__init__(name='eye-csv', daemon=True)
        self.path = path
        self.rows = 0
        self.dropped = 0
        self.queue: 'queue.Queue[Optional[str]]' = queue.Queue(maxsize=max_queue)
        self.file = open(path, 'a', newline='', encoding='utf-8-sig')
        self.writer = csv.writer(self.file)
        if os.path.getsize(path) == 0:
            self.writer.writerow(rec_mod.HEADER_JSON)

    def submit(self, text: str) -> None:
        """接收线程调用：只入队，不落盘、不解析。"""
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            self.dropped += 1

    def run(self) -> None:
        while True:
            try:
                text = self.queue.get(timeout=0.5)
            except queue.Empty:
                try:
                    self.file.flush()
                except OSError:
                    pass
                continue
            if text is None:                       # 收尾信号
                break
            try:
                rows, _flavor = rec_mod.rows_of(text)
            except Exception:                      # noqa: BLE001 - 单帧坏掉不能拖垮录制
                continue
            for row in rows:
                self.writer.writerow(row)
                self.rows += 1
            if self.rows % 200 == 0:
                try:
                    self.file.flush()              # 定期刷盘，断电最多丢 200 帧
                except OSError:
                    pass
        try:
            self.file.flush()
            self.file.close()
        except OSError:
            pass

    def close(self) -> None:
        """停止：把队列里剩下的写完再关文件（最多等 3 秒）。"""
        try:
            self.queue.put(None, timeout=1.0)
        except queue.Full:
            pass
        self.join(timeout=3.0)
        if self.is_alive():                        # 极端情况：直接关文件，别再等
            try:
                self.file.close()
            except OSError:
                pass


class ReplayServer(threading.Thread):
    """把 CSV 当录音带重放成本机 TCP 流（回放测试页用）。"""

    def __init__(self, frames, port: int, on_log, speed: float = 1.0):
        super().__init__(name='eye-replay', daemon=True)
        self.frames = frames
        self.port = port
        self.on_log = on_log
        self.speed = max(speed, 0.1)
        self.stop_event = threading.Event()
        self.sent = 0
        self.total = len(frames)
        self._srv: Optional[socket.socket] = None

    def run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', self.port))
            srv.listen(1)
            srv.settimeout(1.0)
            self._srv = srv
            self.on_log('回放服务已监听 127.0.0.1:{}，等待接收端连入'.format(self.port))
            conn = None
            while conn is None and not self.stop_event.is_set():
                try:
                    conn, peer = srv.accept()
                except socket.timeout:
                    continue
            if conn is None:
                return
            self.on_log('{} 已连入，开始发送 {} 帧'.format(peer, self.total))
            base_dev = self.frames[0][0] if self.frames else 0
            t0 = time.time()
            for dev_ms, payload in self.frames:
                if self.stop_event.is_set():
                    break
                due = (dev_ms - base_dev) / 1000.0 / self.speed
                wait = due - (time.time() - t0)
                if wait > 0:
                    time.sleep(min(wait, 0.5))
                try:
                    conn.sendall(payload)
                except OSError as exc:
                    self.on_log('发送中断：{}'.format(exc))
                    break
                self.sent += 1
            try:
                conn.shutdown(socket.SHUT_WR)
                time.sleep(0.2)
                conn.close()
            except OSError:
                pass
            self.on_log('回放结束，已发送 {} 帧'.format(self.sent))
        except OSError as exc:
            self.on_log('回放服务失败：{}'.format(exc))
        finally:
            if self._srv is not None:
                try:
                    self._srv.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self.stop_event.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass


class Toolbox:
    """一个窗口四个页签；同一时刻只连一个设备连接，避免两页抢同一个 socket。"""

    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.data_dir, self.data_dir_source = live_mod.resolve_data_dir(args.data_dir)
        self.grace = float(getattr(args, 'grace', 60.0))
        self.watcher = live_mod.SessionWatcher(self.data_dir, args.db or '', grace=self.grace)
        self.sink: Optional[live_mod.DbSink] = None
        self.recorder: Optional[CsvWriter] = None
        self.replay: Optional[ReplayServer] = None
        self.link: Optional[RawLink] = None
        self.mode = ''                  # '' | 'live' | 'csv'
        self.logs: 'queue.Queue[str]' = queue.Queue()
        self.import_results: 'queue.Queue[tuple]' = queue.Queue()

        self.frames = 0
        self.written_session = 0
        self.skipped_dup = 0
        self.dropped_no_db = 0
        self.csv_rows = 0
        self.db_path = ''
        self.offset: Optional[float] = None
        self._auto_name = ''            # 上一次自动生成的文件名，用来判断用户是否手改过

        self._build()
        self._refresh_csv_name(force=True)
        if getattr(args, 'no_save_csv', False):
            self.save_csv_var.set(False)
        elif getattr(args, 'save_csv', False):
            self.save_csv_var.set(True)
        self._tick()

    # ------------------------------------------------------------------ 界面
    def _build(self) -> None:
        self.root.title('眼动工具箱')
        style = ttk.Style()
        try:
            style.theme_use('vista')
        except tk.TclError:
            pass

        head = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        head.pack(fill='x')
        head.columnconfigure(5, weight=1)
        saved = live_mod.load_settings()
        self.host_var = tk.StringVar(value=self.args.host or str(saved.get('host') or live_mod.DEFAULT_HOST))
        self.port_var = tk.StringVar(value=str(self.args.port or saved.get('port') or live_mod.DEFAULT_PORT))
        ttk.Label(head, text='设备').grid(row=0, column=0, sticky='w')
        ttk.Entry(head, textvariable=self.host_var, width=15).grid(row=0, column=1, padx=(4, 10))
        ttk.Label(head, text='端口').grid(row=0, column=2, sticky='w')
        ttk.Entry(head, textvariable=self.port_var, width=7).grid(row=0, column=3, padx=(4, 10))
        ttk.Label(head, text='数据目录').grid(row=0, column=4, sticky='w')
        self.dir_var = tk.StringVar(value=self.data_dir)
        ttk.Entry(head, textvariable=self.dir_var).grid(row=0, column=5, sticky='ew', padx=4)
        ttk.Button(head, text='浏览…', command=self._browse).grid(row=0, column=6)
        self.source_label = ttk.Label(self.root, text='目录来源：{}'.format(self.data_dir_source),
                                      foreground='#777777')
        self.source_label.pack(anchor='w', padx=14)

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill='both', expand=True, padx=12, pady=6)
        self._build_live_tab()
        self._build_csv_tab()
        self._build_import_tab()
        self._build_replay_tab()

        self.log_box = tk.Text(self.root, height=6, wrap='word', font=('Consolas', 9),
                               state='disabled', relief='flat', background='#f7f7f7')
        self.log_box.pack(fill='both', expand=False, padx=12, pady=(0, 10))

        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 1080)
        height = max(self.root.winfo_reqheight(), 620)
        self.root.geometry('{}x{}'.format(width, height))
        self.root.minsize(width, height)

    def _cards(self, parent) -> Dict[str, ttk.Label]:
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=(6, 0))
        values: Dict[str, ttk.Label] = {}
        for col, (key, title, init) in enumerate(
                (('frames', '收帧', '0'), ('written', '已入库', '0'),
                 ('rate', '帧率', '0.0'), ('size', '库大小', '0 KB'),
                 ('updated', '最后更新', '—'))):
            row.columnconfigure(col, weight=1, uniform='card')
            box = ttk.LabelFrame(row, text=title, padding=(8, 3))
            box.grid(row=0, column=col, sticky='nsew', padx=4)
            label = ttk.Label(box, text=init, font=('Consolas', 12, 'bold'))
            label.pack(anchor='w')
            values[key] = label
        return values

    def _build_live_tab(self) -> None:
        page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(page, text='实时入库')
        self.live_state = ttk.Label(page, text='● 未开始', font=('Microsoft YaHei', 12, 'bold'),
                                    foreground='#888888')
        self.live_state.pack(anchor='w')
        self.live_db = ttk.Label(page, text='目标库：—', font=('Consolas', 9))
        self.live_db.pack(anchor='w', pady=(2, 0))
        self.live_values = self._cards(page)
        bar = ttk.Frame(page)
        bar.pack(fill='x', pady=(10, 0))
        self.live_button = ttk.Button(bar, text='开始接收', command=self.start_live)
        self.live_button.pack(side='left')
        ttk.Button(bar, text='切换库…', command=self._choose_db).pack(side='left', padx=8)
        ttk.Button(bar, text='打开库所在文件夹', command=self._open_dir).pack(side='left')
        self.save_csv_var = tk.BooleanVar(value=True)
        self.save_csv_check = ttk.Checkbutton(bar, text='同时保存 CSV',
                                              variable=self.save_csv_var)
        self.save_csv_check.pack(side='left', padx=(12, 0))
        self.live_csv_label = ttk.Label(page, text='', foreground='#666666')
        self.live_csv_label.pack(anchor='w', pady=(4, 0))
        ttk.Label(page, text='写入主程序「当前正在记录」的会话库（eye_data 表）；'
                             '只在这个库最近有新数据时才写',
                  foreground='#666666').pack(anchor='w', pady=(6, 0))

    def _build_csv_tab(self) -> None:
        page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(page, text='录制 CSV')
        form = ttk.Frame(page)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='保存到').grid(row=0, column=0, sticky='w')
        self.csv_dir = tk.StringVar(value=self.data_dir)
        ttk.Entry(form, textvariable=self.csv_dir).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(form, text='浏览…', command=self._browse_csv).grid(row=0, column=2)
        ttk.Label(form, text='文件名').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.csv_name = tk.StringVar(value='')
        ttk.Entry(form, textvariable=self.csv_name).grid(row=1, column=1, sticky='ew', padx=4,
                                                         pady=(6, 0))
        ttk.Button(form, text='自动命名',
                   command=lambda: self._refresh_csv_name(force=True)).grid(row=1, column=2,
                                                                           pady=(6, 0))
        self.csv_values = self._cards(page)
        bar = ttk.Frame(page)
        bar.pack(fill='x', pady=(10, 0))
        self.csv_button = ttk.Button(bar, text='开始录制', command=self.start_csv)
        self.csv_button.pack(side='left')
        ttk.Button(bar, text='打开所在文件夹',
                   command=lambda: self._open_folder(self.csv_dir.get())).pack(side='left', padx=8)
        ttk.Label(page, text='与实时入库共用同一个设备连接；格式与 EyeRec 完全一致（27 列），'
                             '可用「导入到库」合并进会话库',
                  foreground='#666666').pack(anchor='w', pady=(8, 0))

    # -------------------------------------------------------------- CSV 文件名
    def _auto_csv_name(self, folder: str = '') -> str:
        """默认名：眼动数据_20261117_230759.csv；同一秒里撞名就加 _2、_3。"""
        folder = folder or self.csv_dir.get().strip() or self.data_dir
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = '眼动数据_{}.csv'.format(stamp)
        index = 2
        while os.path.exists(os.path.join(folder, name)):
            name = '眼动数据_{}_{}.csv'.format(stamp, index)
            index += 1
        return name

    def _refresh_csv_name(self, force: bool = False) -> None:
        """填自动名。用户手改过（当前值既不空也不等于上次自动名）就不动他改的。"""
        current = self.csv_name.get().strip()
        if not force and current and current != self._auto_name:
            return
        self._auto_name = self._auto_csv_name()
        self.csv_name.set(self._auto_name)

    def _csv_target_text(self) -> str:
        """没在采集时，实时入库页那行先告诉人这次会存到哪。"""
        if not self.save_csv_var.get():
            return 'CSV：本次不保存（未勾选）'
        folder = self.csv_dir.get().strip() or self.data_dir
        name = self.csv_name.get().strip() or self._auto_csv_name(folder)
        path = os.path.join(folder, name)
        return 'CSV 将保存到：{}'.format(live_mod.LiveWindow._short_path(path))

    def _set_csv_text(self, text: str) -> None:
        if self.live_csv_label.cget('text') != text:
            self.live_csv_label.configure(text=text)

    def _build_import_tab(self) -> None:
        page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(page, text='导入到库')
        form = ttk.Frame(page)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='会话目录').grid(row=0, column=0, sticky='w')
        self.import_dir = tk.StringVar(value=self.data_dir)
        ttk.Entry(form, textvariable=self.import_dir).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(form, text='浏览…', command=self._browse_import).grid(row=0, column=2)
        bar = ttk.Frame(page)
        bar.pack(fill='x', pady=(8, 0))
        ttk.Button(bar, text='检查配对（不写库）', command=self.import_check).pack(side='left')
        self.import_write_button = ttk.Button(bar, text='确认写入', command=self.import_write,
                                              state='disabled')
        self.import_write_button.pack(side='left', padx=8)
        self.import_report = tk.Text(page, height=12, wrap='word', font=('Consolas', 9),
                                     relief='flat', background='#fbfbfb')
        self.import_report.pack(fill='both', expand=True, pady=(8, 0))
        ttk.Label(page, text='CSV 会自动在会话目录及其上级里按时间窗重叠挑选；'
                             '目标库该时段已有数据时会拒绝写入，防止重复导入',
                  foreground='#666666').pack(anchor='w', pady=(6, 0))

    def _build_replay_tab(self) -> None:
        page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(page, text='回放测试')
        form = ttk.Frame(page)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='CSV 文件').grid(row=0, column=0, sticky='w')
        self.replay_csv = tk.StringVar(value='')
        ttk.Entry(form, textvariable=self.replay_csv).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(form, text='选择…', command=self._browse_replay).grid(row=0, column=2)
        ttk.Label(form, text='监听端口').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.replay_port = tk.StringVar(value='17901')
        ttk.Entry(form, textvariable=self.replay_port, width=8).grid(row=1, column=1, sticky='w',
                                                                     padx=4, pady=(6, 0))
        bar = ttk.Frame(page)
        bar.pack(fill='x', pady=(10, 0))
        self.replay_button = ttk.Button(bar, text='开始回放', command=self.start_replay)
        self.replay_button.pack(side='left')
        self.replay_label = ttk.Label(bar, text='未开始', foreground='#777777')
        self.replay_label.pack(side='left', padx=12)
        ttk.Label(page, text='把录好的 CSV 当“录音带”重放成 TCP 流，给接收端/界面做联调；'
                             '设备不在手边时用这个验证',
                  foreground='#666666').pack(anchor='w', pady=(10, 0))

    # ------------------------------------------------------------------ 通用操作
    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title='选择 data 目录', initialdir=self.dir_var.get())
        if not chosen:
            return
        self.dir_var.set(chosen)
        self.data_dir = chosen
        self.data_dir_source = '手动选择'
        self.source_label.configure(text='目录来源：手动选择')
        self.watcher = live_mod.SessionWatcher(self.data_dir, '', grace=self.grace)
        live_mod.save_settings(data_dir=chosen)
        self.log('数据目录改为：{}'.format(chosen))
        self.csv_dir.set(chosen)
        self.import_dir.set(chosen)

    def _browse_csv(self) -> None:
        chosen = filedialog.askdirectory(title='CSV 保存到哪个文件夹',
                                         initialdir=self.csv_dir.get() or self.data_dir)
        if chosen:
            self.csv_dir.set(chosen)

    def _browse_import(self) -> None:
        chosen = filedialog.askdirectory(title='选择会话目录（含会话库）',
                                         initialdir=self.import_dir.get() or self.data_dir)
        if chosen:
            self.import_dir.set(chosen)

    def _browse_replay(self) -> None:
        path = filedialog.askopenfilename(title='选择要回放的 CSV',
                                          initialdir=self.data_dir,
                                          filetypes=[('CSV', '*.csv'), ('全部', '*.*')])
        if path:
            self.replay_csv.set(path)

    def _open_dir(self) -> None:
        self._open_folder(os.path.dirname(self.db_path) if self.db_path else self.data_dir)

    @staticmethod
    def _open_folder(folder: str) -> None:
        try:
            os.startfile(folder)                   # type: ignore[attr-defined]
        except Exception:                          # noqa: BLE001
            pass

    def _choose_db(self) -> None:
        rows = self.watcher.candidates()
        if not rows:
            messagebox.showinfo('切换库', '这个目录下还没有会话库：\n{}'.format(self.data_dir))
            return
        win = tk.Toplevel(self.root)
        win.title('选择会话库')
        win.transient(self.root)
        ttk.Label(win, text='按最后数据时间排序', padding=(10, 8)).pack(anchor='w')
        box = tk.Listbox(win, width=90, height=min(12, len(rows)), font=('Consolas', 9))
        for path, latest, mtime in rows:
            stamp = datetime.fromtimestamp(latest).strftime('%m-%d %H:%M:%S') if latest else '无数据'
            box.insert('end', '{}　最后数据 {}　文件 {}'.format(
                os.path.relpath(path, self.data_dir), stamp,
                datetime.fromtimestamp(mtime).strftime('%H:%M:%S')))
        box.selection_set(0)
        box.pack(fill='both', expand=True, padx=10)

        def confirm():
            index = box.curselection()
            if index:
                path = rows[index[0]][0]
                self.watcher = live_mod.SessionWatcher(self.data_dir, path, grace=self.grace)
                live_mod.save_settings(pinned_db=path, data_dir=self.data_dir)
                self.log('已钉住库：{}'.format(os.path.basename(path)))
            win.destroy()

        bar = ttk.Frame(win, padding=8)
        bar.pack(fill='x')
        ttk.Button(bar, text='就用这个', command=confirm).pack(side='right')
        ttk.Button(bar, text='取消', command=win.destroy).pack(side='right', padx=8)
        ttk.Button(bar, text='恢复自动',
                   command=lambda: (self._unpin(), win.destroy())).pack(side='left')

    def _unpin(self) -> None:
        self.watcher = live_mod.SessionWatcher(self.data_dir, '', grace=self.grace)
        live_mod.save_settings(pinned_db='')
        self.log('已恢复自动跟随最新库')

    # ------------------------------------------------------------------ 连接
    def _ensure_link(self) -> bool:
        if self.link is not None and self.link.is_alive():
            return True
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.log('端口不是数字：{}'.format(self.port_var.get()))
            return False
        live_mod.save_settings(host=host, port=port, data_dir=self.dir_var.get())
        self.link = RawLink(host, port, self._on_frame, self.log, self._on_raw)
        self.link.start()
        self.log('连接 {}:{}'.format(host, port))
        return True

    def _drop_link(self) -> None:
        if self.link is not None:
            self.link.stop()
            self.link = None

    def start_live(self) -> None:
        if self.mode == 'live':
            self.stop_all('实时入库已停止')
            return
        self.stop_all()
        if not self._ensure_link():
            return
        self.mode = 'live'
        self.live_button.configure(text='停止')
        self.csv_button.configure(text='开始录制')
        self.save_csv_check.configure(state='disabled')
        if self.save_csv_var.get():
            folder = self.csv_dir.get().strip() or self.data_dir
            name = self.csv_name.get().strip() or self._auto_csv_name(folder)
            self.csv_name.set(name)
            if os.path.exists(os.path.join(folder, name)):
                self.log('同名 CSV 已存在，本次追加写入：{}'.format(name))
            try:
                os.makedirs(folder, exist_ok=True)
                self.recorder = CsvWriter(os.path.join(folder, name))
                self.recorder.start()
                self.log('同时保存 CSV → {}'.format(self.recorder.path))
            except OSError as exc:
                self.recorder = None
                self.log('CSV 打不开，本次只写库：{}'.format(exc))
        self.log('开始实时入库；写入 {}'.format(self.data_dir))

    def start_csv(self) -> None:
        if self.mode == 'csv':
            self.stop_all('录制已停止')
            return
        self.stop_all()
        folder = self.csv_dir.get().strip() or self.data_dir
        name = self.csv_name.get().strip() or self._auto_csv_name(folder)
        self.csv_name.set(name)
        if os.path.exists(os.path.join(folder, name)):
            self.log('同名 CSV 已存在，本次追加写入：{}'.format(name))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.log('建目录失败：{}'.format(exc))
            return
        path = os.path.join(folder, name)
        try:
            self.recorder = CsvWriter(path)
            self.recorder.start()
        except OSError as exc:
            self.log('打不开 CSV：{}'.format(exc))
            return
        if not self._ensure_link():
            self.recorder.close()
            self.recorder = None
            return
        self.mode = 'csv'
        self.csv_rows = 0
        self.csv_button.configure(text='停止录制')
        self.live_button.configure(text='开始接收')
        self.log('开始录制 → {}'.format(path))

    def stop_all(self, message: str = '') -> None:
        was_running = bool(self.mode or self.recorder is not None)
        self.mode = ''
        self.live_button.configure(text='开始接收')
        self.csv_button.configure(text='开始录制')
        self.save_csv_check.configure(state='normal')
        if self.recorder is not None:
            self.recorder.close()                  # 等队列写完再关文件
            extra = '，队列满丢弃 {} 帧'.format(self.recorder.dropped) if self.recorder.dropped else ''
            self.log('CSV 结束，共 {} 行{}'.format(self.recorder.rows, extra))
            self.csv_rows = self.recorder.rows
            self.recorder = None
        if self.sink is not None:
            self.sink.close()
            self.written_session = self.sink.written
            self.skipped_dup = self.sink.skipped_dup
            self.sink = None
        self._drop_link()
        if was_running:                            # 真的录过才换新时间戳，空转别改名字
            self._refresh_csv_name()
        self._set_csv_text(self._csv_target_text())
        if message:
            self.log(message)

    # ------------------------------------------------------------------ 数据
    def _on_frame(self, obj: Dict[str, object]) -> None:
        self.frames += 1
        if self.mode == 'live':
            self._write_live(obj)

    _num = staticmethod(live_mod.LiveWindow._num)          # 复用同一套 -1 判无效
    _build_value = live_mod.LiveWindow._build_value        # 同一份 value 结构

    def _on_raw(self, text: str) -> None:
        """录制模式：把原始报文写成 CSV（表头/列序与 EyeRec 一致）。"""
        recorder = self.recorder
        if recorder is None:
            return
        recorder.submit(text)
        self.csv_rows = recorder.rows

    def _write_live(self, obj: Dict[str, object]) -> None:
        if self.mode != 'live':
            return
        sink = self.sink
        try:
            dev_ms = int(float(obj.get('timestamp', 0)))
        except (TypeError, ValueError):
            dev_ms = 0
        now = time.time()
        if dev_ms:
            sample = now - dev_ms / 1000.0
            if self.offset is None or abs(sample - self.offset) > 5.0:
                self.offset = sample
            else:
                self.offset = self.offset * 0.98 + sample * 0.02
            stamp = dev_ms / 1000.0 + self.offset
        else:
            stamp = now
        if sink is None:
            self.dropped_no_db += 1
            return
        try:
            sink.add(datetime.fromtimestamp(stamp).strftime(TIME_FORMAT),
                     self._build_value(obj, dev_ms))
            sink.maybe_flush()
            self.written_session = sink.written
            self.skipped_dup = sink.skipped_dup
        except Exception as exc:                   # noqa: BLE001
            self.log('写库失败：{}'.format(exc))

    # ------------------------------------------------------------------ 导入
    def import_check(self) -> None:
        self._run_import(write=False)

    def import_write(self) -> None:
        if not messagebox.askyesno('确认写入', '把眼动数据写进上面这个会话库？'):
            return
        self._run_import(write=True)

    def _run_import(self, write: bool) -> None:
        folder = self.import_dir.get().strip() or self.data_dir
        argv = ['--db', folder]
        if write:
            argv.append('--write')
        buffer = io.StringIO()

        def work():
            try:
                with contextlib.redirect_stdout(buffer):
                    code = import_mod._run(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
            except Exception as exc:               # noqa: BLE001
                buffer.write('\n[错误] {}\n'.format(exc))
                code = 9
            # 不能在工作线程里碰控件或调用 root.after（tkinter 要求主线程），
            # 结果丢进队列，由 _tick 在主线程取出来渲染
            self.import_results.put((buffer.getvalue(), code, write))

        threading.Thread(target=work, daemon=True).start()
        self.import_report.delete('1.0', 'end')
        self.import_report.insert('end', '正在检查…\n')

    def _show_import(self, text: str, code: int, write: bool) -> None:
        self.import_report.delete('1.0', 'end')
        self.import_report.insert('end', text)
        ok = code == 0
        # 只有「检查通过」才允许点写入；写完再检查时保持可用，方便继续看
        self.import_write_button.configure(state='normal' if (ok or write) else 'disabled')

    # ------------------------------------------------------------------ 回放
    def start_replay(self) -> None:
        if self.replay is not None and self.replay.is_alive():
            self.replay.stop()
            self.replay = None
            self.replay_button.configure(text='开始回放')
            self.replay_label.configure(text='已停止')
            return
        path = self.replay_csv.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showinfo('回放测试', '先选一个 CSV 文件')
            return
        try:
            frames = replay_mod.load_frames(path)
        except SystemExit as exc:
            self.log('读 CSV 失败：{}'.format(exc))
            return
        try:
            port = int(self.replay_port.get())
        except ValueError:
            self.log('端口不是数字')
            return
        self.replay = ReplayServer(frames, port, self.log)
        self.replay.start()
        self.replay_button.configure(text='停止回放')

    # ------------------------------------------------------------------ 定时
    def _tick(self) -> None:
        try:
            self._refresh_db()
            self._refresh_labels()
            self._drain_import()
            self._drain_logs()
        finally:
            self.root.after(REFRESH_MS, self._tick)

    def _drain_import(self) -> None:
        while True:
            try:
                text, code, write = self.import_results.get_nowait()
            except queue.Empty:
                return
            self._show_import(text, code, write)

    def _refresh_db(self) -> None:
        if self.mode != 'live':
            return
        path = self.watcher.poll()
        write_ok = bool(path) and (self.watcher.live or bool(self.watcher.pinned))
        if not write_ok:
            if self.sink is not None:
                self.sink.close()
                self.written_session = self.sink.written
                self.skipped_dup = self.sink.skipped_dup
                self.sink = None
                self.log('暂停写入：{} 最近没有新数据'.format(
                    os.path.basename(path) if path else '（没有库）'))
            self.db_path = path
            return
        if path != self.db_path or self.sink is None:
            if self.sink is not None:
                self.sink.close()
                self.log('切换目标库 → {}'.format(os.path.basename(path)))
            try:
                self.sink = live_mod.DbSink(path)
            except Exception as exc:               # noqa: BLE001
                self.sink = None
                self.log('打不开库 {}：{}'.format(os.path.basename(path), exc))
                return
            self.db_path = path
            self.written_session = 0
            self.skipped_dup = 0
            self.offset = None
            self.log('锁定目标库：{}'.format(path))

    def _refresh_labels(self) -> None:
        link = self.link
        connected = bool(link and link.stats['connected'])
        latest = self.watcher.current_max
        age = (time.time() - latest) if latest else 999

        if self.mode != 'live':
            text, color = '● 未开始' if not self.mode else '● 实时入库未开（当前在录制 CSV）', '#888888'
        elif self.db_path and self.sink is None:
            text, color = '● 找到库但没在采（{}）'.format(
                datetime.fromtimestamp(latest).strftime('%H:%M:%S') if latest else '无数据'), '#c27c00'
        elif self.sink is None:
            text, color = '● 等待会话库（主程序还没开始采集）', '#c27c00'
        elif not connected:
            text, color = '● 连接中…', '#c27c00'
        elif not (link and link.stats['parsed']):
            text, color = '● 已连接，等数据', '#c27c00'
        else:
            text, color = '● 正在入库', '#1a7f37'
        if self.dropped_no_db:
            text += '（{} 帧无库可写）'.format(self.dropped_no_db)
        self.live_state.configure(text=text, foreground=color)

        if self.db_path:
            state = '主程序正在写' if self.watcher.growing else '主程序 {} 秒前停写'.format(int(age))
            self.live_db.configure(text='目标库：{}　（{}）'.format(
                live_mod.LiveWindow._short_path(self.db_path), state))
        else:
            self.live_db.configure(text='目标库：—（{} 下没找到会话库）'.format(self.data_dir))

        if self.mode == 'live' and self.recorder is not None:
            self._set_csv_text('CSV：{} 行 → {}'.format(
                self.recorder.rows, self.recorder.path))
        elif self.mode == 'live':
            self._set_csv_text('CSV：本次不保存')
        else:
            self._set_csv_text(self._csv_target_text())

        elapsed = max(time.time() - (link.stats['started_at'] if link else time.time()), 0.001)
        rate = (link.stats['parsed'] / elapsed) if link else 0.0
        size = self.sink.size if self.sink else 0
        stamp = link.stats['last_at'] if link else 0
        for values, written in ((self.live_values, self.written_session),
                                (self.csv_values, self.csv_rows)):
            values['frames'].configure(text=str(self.frames))
            values['written'].configure(text=str(written))
            values['rate'].configure(text='{:.1f}'.format(rate))
            values['size'].configure(text='{:.0f} KB'.format(size / 1024) if size < 1024 * 1024
                                     else '{:.1f} MB'.format(size / 1024 / 1024))
            values['updated'].configure(
                text=datetime.fromtimestamp(stamp).strftime('%H:%M:%S') if stamp else '—')

        if self.replay is not None:
            self.replay_label.configure(text='已发送 {} / {} 帧'.format(
                self.replay.sent, self.replay.total))

    def _drain_logs(self) -> None:
        changed = False
        while True:
            try:
                line = self.logs.get_nowait()
            except queue.Empty:
                break
            self.log_box.configure(state='normal')
            self.log_box.insert('end', '{}\n'.format(line))
            self.log_box.see('end')
            self.log_box.configure(state='disabled')
            changed = True
        if changed:
            content = self.log_box.get('1.0', 'end').splitlines()
            if len(content) > 200:
                self.log_box.configure(state='normal')
                self.log_box.delete('1.0', '{}.0'.format(len(content) - 200))
                self.log_box.configure(state='disabled')

    def log(self, message: str) -> None:
        self.logs.put('[{}] {}'.format(datetime.now().strftime('%H:%M:%S'), message))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='眼动工具箱（一个窗口四个功能）')
    ap.add_argument('--host', default=None)
    ap.add_argument('-p', '--port', type=int, default=None)
    ap.add_argument('--db', default='', help='钉死写某个会话库；不给就自动跟随')
    ap.add_argument('--data-dir', default='')
    ap.add_argument('--grace', type=float, default=60.0)
    ap.add_argument('--save-csv', action='store_true',
                    help='默认已勾选，此开关保留兼容（可省略）')
    ap.add_argument('--no-save-csv', action='store_true',
                    help='启动时取消勾选「同时保存 CSV」')
    ap.add_argument('--selftest', action='store_true', help='无窗口自检')
    args = ap.parse_args(argv)

    live_mod._enable_dpi_awareness()
    live_mod._install_error_log()
    root = tk.Tk()
    app = Toolbox(root, args)

    if args.selftest:
        app.start_live()
        deadline = time.time() + 4.0
        while time.time() < deadline:
            root.update()
            time.sleep(0.05)
        print('自检：目录={}（{}）｜收帧 {}｜入库 {}｜跳过重复 {}｜无库丢弃 {}｜库 {}'.format(
            app.data_dir, app.data_dir_source, app.frames, app.written_session,
            app.skipped_dup, app.dropped_no_db, os.path.basename(app.db_path) or '无'))
        app.stop_all()
        root.destroy()
        return 0 if app.written_session > 0 else 1

    app.start_live()
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
