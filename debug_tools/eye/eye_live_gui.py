# -*- coding: utf-8 -*-
"""眼动实时入库小界面：连设备 → 找「正在记录」的会话库 → 边收边写 eye_data。

不依赖主程序、不修改主程序，只用标准库（tkinter + sqlite3 + socket）。

**怎么保证找对库**（换电脑、换打包版都要成立）：
  1. 数据目录不写死，按顺序探测：命令行 --data-dir → 上次用的（记住在同目录
     `.eye_live_gui.json`）→ 源码目录 data → 打包版 dist/HcpsMonitor/data →
     当前目录 data。用了哪个会在界面「数据目录」后面标出来。
  2. 判「哪个库在写」不看文件时间，看**数据有没有增长**：读各表最后一行的 time，
     两次轮询之间变大才算正在记录。mtime 会被拷贝、杀毒、打开预览污染，不可靠。
  3. 同时有两个会话库都在增长（双开主程序）→ 不猜，弹选择框让你点，并把选择记住。

用法：
    python debug_tools/eye/eye_live_gui.py                     # 自动探测
    python debug_tools/eye/eye_live_gui.py --data-dir D:\\x\\data
    python debug_tools/eye/eye_live_gui.py --db ...\\20261117_211005.db   # 钉死一个库
    python debug_tools/eye/eye_live_gui.py --selftest --data-dir _tmp     # 自检
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'AAAwindows_code')            # AAAwindows_code
TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
EYE_TABLE = 'eye_data'
DEFAULT_HOST = '192.168.2.98'
DEFAULT_PORT = 7901
# 打包后 __file__ 在 _internal 里，不能拿它推配置位置；放在 exe 旁边便于整个文件夹拷走
SETTINGS_PATH = (os.path.join(os.path.dirname(os.path.abspath(sys.executable)), '.eye_live_gui.json')
                 if getattr(sys, 'frozen', False) else os.path.join(HERE, '.eye_live_gui.json'))
# 用哪几张表的最后一行判断"有没有新数据"（按主程序实际会写的表）
PROBE_TABLES = ('ecg_data', 'bracelet_ppg_data', 'bracelet_gsr_data',
                'bracelet_accel_data', 'camera_frames')
SKIP_DIRS = ('frames', 'video', '_frames')


def _enable_dpi_awareness() -> None:
    """声明 DPI 感知：不声明的话，150% 缩放下整个窗口会被系统位图放大，字发虚。"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)     # 每显示器感知
        except Exception:                                      # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()          # 老系统兜底
    except Exception:                                          # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 配置记忆
# ---------------------------------------------------------------------------
def load_settings() -> Dict[str, object]:
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes) -> None:
    data = load_settings()
    data.update({k: v for k, v in changes.items() if v is not None})
    try:
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 找数据目录 / 找库
# ---------------------------------------------------------------------------
def _base_roots() -> List[Tuple[str, str]]:
    """可能放着 data 的根目录 [(根, 说明)]，按优先级排列。

    打包后 __file__ 落在 _internal 里，不能用它推项目根 —— 必须用 exe 所在目录，
    否则换台电脑、换个摆放位置就找不到库。
    """
    roots: List[Tuple[str, str]] = []
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.append((exe_dir, 'exe 同级'))
        roots.append((os.path.dirname(exe_dir), 'exe 上一级'))
    else:
        roots.append((PROJECT, '源码目录'))

    seen = {os.path.normcase(path) for path, _label in roots}
    base = roots[0][0]
    for _ in range(4):                     # 兼容「工具包放在程序子目录里」
        base = os.path.dirname(base)
        if not base or os.path.normcase(base) in seen:
            break
        seen.add(os.path.normcase(base))
        roots.append((base, '上级目录'))
    return roots


def resolve_data_dir(explicit: str = '') -> Tuple[str, str]:
    """返回 (目录, 来源说明)。已经在用的目录优先，避免换机器后不认路。"""
    if explicit:
        return os.path.abspath(explicit), '命令行指定'

    saved = str(load_settings().get('data_dir') or '')
    if saved and os.path.isdir(saved):
        return saved, '上次使用'

    candidates: List[Tuple[str, str]] = []
    seen: set = set()

    def add(path: str, label: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        candidates.append((path, label))

    # 每一层都试三种常见布局：工具包可能放在项目根、程序目录或 dist 里
    for root, label in _base_roots():
        add(os.path.join(root, 'data'), label + ' / data')
        add(os.path.join(root, 'AAAwindows_code', 'data'), label + ' / AAAwindows_code\\data')
        add(os.path.join(root, 'dist', 'HcpsMonitor', 'data'), label + ' / dist\\HcpsMonitor\\data')
    add(os.path.join(os.getcwd(), 'data'), '当前目录 / data')
    existing = [(path, label) for path, label in candidates if os.path.isdir(path)]
    if not existing:
        return candidates[0][0], '默认（目录还不存在）'
    # 多个都存在时，选「最新库更近」的那个：源码目录和打包产物常常同时存在
    def freshness(item):
        newest = 0.0
        for path, _mtime in list_session_dbs(item[0]):
            newest = max(newest, _mtime)
        return newest
    existing.sort(key=freshness, reverse=True)
    path, label = existing[0]
    if len(existing) > 1:
        label += '（另有 {} 个候选）'.format(len(existing) - 1)
    return path, label


def list_session_dbs(data_dir: str) -> List[Tuple[str, float]]:
    """data 下所有会话库 [(路径, mtime)]，排除独立的 camera_*.db。"""
    out = []
    if not os.path.isdir(data_dir):
        return out
    for folder, dirs, files in os.walk(data_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.lower().endswith('.db') or name.lower().startswith('camera_'):
                continue
            path = os.path.join(folder, name)
            try:
                out.append((path, os.path.getmtime(path)))
            except OSError:
                continue
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def probe_last_time(path: str) -> float:
    """读这个库里最新一条数据的时间（epoch）。读不到返回 0。"""
    try:
        conn = sqlite3.connect('file:{}?mode=ro'.format(path), uri=True, timeout=2)
    except sqlite3.Error:
        return 0.0
    best = 0.0
    try:
        for table in PROBE_TABLES:
            try:
                row = conn.execute(
                    'SELECT time FROM "{}" ORDER BY rowid DESC LIMIT 1'.format(table)).fetchone()
            except sqlite3.Error:
                continue
            if not row or not row[0]:
                continue
            try:
                best = max(best, datetime.strptime(row[0][:26], TIME_FORMAT).timestamp())
            except ValueError:
                continue
    finally:
        conn.close()
    return best


class SessionWatcher:
    """判断「现在哪个库在写」：以数据时间增长为准，mtime 只作参考。"""

    def __init__(self, data_dir: str, pinned: str = '', grace: float = 60.0):
        self.data_dir = data_dir
        self.pinned = pinned
        self.grace = float(grace)       # 最后一条数据距今多久之内算「还在采」
        self.current = ''
        self.current_max = 0.0
        self.growing = False
        self.live = False               # 这个库现在是不是真的在收数据
        self.ambiguous: List[str] = []
        self._seen: Dict[str, float] = {}       # 路径 → 上次看到的最后数据时间

    def poll(self) -> str:
        if self.pinned:
            path = self.pinned
            latest = probe_last_time(path)
            self._update(path, latest)
            self.ambiguous = []
            self.live = True            # 手动钉住就无条件写，尊重用户选择
            return path

        scored = []                     # [(最后数据时间, mtime, 路径)]
        for path, mtime in list_session_dbs(self.data_dir):
            latest = probe_last_time(path)
            scored.append((latest, mtime, path))
        if not scored:
            self.current, self.current_max, self.growing = '', 0.0, False
            self.live = False
            self.ambiguous = []
            return ''

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        now = time.time()
        live = [item for item in scored if now - item[0] < 10.0]
        self.ambiguous = [item[2] for item in live] if len(live) > 1 else []

        best = scored[0]
        current_row = next((item for item in scored if item[2] == self.current), None)

        # 迟滞：一旦锁定就不再跟着"最新"乱跳。双开主程序时两个库都在长，
        # 若按最大值切换，数据会被交替写进两个库 —— 比选错更糟。
        path = self.current
        if current_row is not None and current_row in live:
            if best[2] != self.current and best[0] > current_row[0] + 2.0:
                path = best[2]                  # 另一个库明显更新（晚 2 秒以上）才换
        elif current_row is not None:
            # 当前库停了：只有唯一一个库在长时才切，避免在多个候选间来回跳
            if len(live) == 1:
                path = live[0][2]
        else:
            path = best[2]                      # 首次锁定 / 原库消失

        latest = next((item[0] for item in scored if item[2] == path), 0.0)
        self._update(path, latest)
        # 写不写由「这个库最近有没有在收数据」决定：选到昨天的会话库时不能往里写
        self.live = bool(latest) and (now - latest) < self.grace
        return path

    def _update(self, path: str, latest: float) -> None:
        prev = self._seen.get(path)
        self.growing = prev is not None and latest > prev
        self._seen[path] = latest
        self.current = path
        self.current_max = latest

    def candidates(self) -> List[Tuple[str, float, float]]:
        """给选择框用：[(路径, 最后数据时间, mtime)]，按新到旧。"""
        rows = []
        for path, mtime in list_session_dbs(self.data_dir):
            rows.append((path, probe_last_time(path), mtime))
        rows.sort(key=lambda item: (item[1], item[2]), reverse=True)
        return rows


# ---------------------------------------------------------------------------
# 写库
# ---------------------------------------------------------------------------
class DbSink:
    """往一个会话库批量写 eye_data；库被切走就关掉重建。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        self.conn.execute('CREATE TABLE IF NOT EXISTS "{}" (time TEXT, value TEXT)'.format(EYE_TABLE))
        self.conn.commit()
        self.written = 0
        self.pending = []
        self.last_flush = time.time()
        self.skipped_dup = 0
        self._prev_value = None

    def add(self, time_str: str, value: Dict[str, object]) -> None:
        text = json.dumps(value, ensure_ascii=False)
        if text == self._prev_value:          # 设备会重发完全相同的帧，跳过
            self.skipped_dup += 1
            return
        self._prev_value = text
        self.pending.append((time_str, text))

    def maybe_flush(self, rows: int = 100, interval: float = 0.5) -> int:
        if not self.pending:
            return 0
        if len(self.pending) < rows and time.time() - self.last_flush < interval:
            return 0
        payload, self.pending = self.pending, []
        try:
            self.conn.executemany(
                'INSERT INTO "{}" (time, value) VALUES (?, ?)'.format(EYE_TABLE), payload)
            self.conn.commit()
            self.written += len(payload)
        except sqlite3.Error:
            self.conn.rollback()
            self.pending = payload + self.pending
            raise
        finally:
            self.last_flush = time.time()
        return len(payload)

    def close(self) -> None:
        try:
            self.maybe_flush(rows=1, interval=0)
        except sqlite3.Error:
            pass
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    @property
    def size(self) -> int:
        total = 0
        for suffix in ('', '-wal', '-shm'):
            try:
                total += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return total


# ---------------------------------------------------------------------------
# 收流
# ---------------------------------------------------------------------------
class EyeLink(threading.Thread):
    """TCP 客户端：连接 → 收字节 → 切 JSON 帧 → 回调。"""

    def __init__(self, host: str, port: int, on_frame, on_log):
        super().__init__(name='eye-live-link', daemon=True)
        self.host, self.port = host, port
        self.on_frame, self.on_log = on_frame, on_log
        self.stop_event = threading.Event()
        self.stats = {'connected': False, 'parsed': 0, 'failed': 0,
                      'reconnects': 0, 'last_error': '', 'last_at': 0.0,
                      'started_at': time.time()}
        self._sock: Optional[socket.socket] = None

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._connect_and_read()
                backoff = 1.0
            except OSError as exc:
                self.stats['connected'] = False
                self.stats['last_error'] = str(exc)
                self.stats['reconnects'] += 1
                if not self.stop_event.is_set():
                    self.on_log('连接失败：{}；{:.0f} 秒后重试'.format(exc, backoff))
                    self.stop_event.wait(backoff)
                    backoff = min(backoff * 2, 5.0)

    def _connect_and_read(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(2.0)
        self._sock = sock
        self.stats['connected'] = True
        self.stats['last_error'] = ''
        self.on_log('已连接 {}:{}'.format(self.host, self.port))
        buf = bytearray()
        while not self.stop_event.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                self.on_log('连接中断：{}'.format(exc))
                break
            if not chunk:
                self.on_log('对端关闭连接')
                break
            buf += chunk
            for text in self._split(buf):
                self._handle(text)
                self.stats['last_at'] = time.time()
        self.stats['connected'] = False
        try:
            sock.close()
        except OSError:
            pass
        self._sock = None

    @staticmethod
    def _split(buf: bytearray) -> List[str]:
        """按花括号配平切 JSON 对象（忽略字符串内的括号）；不完整就留着等下一包。"""
        out = []
        while True:
            start = buf.find(b'{')
            if start < 0:
                buf.clear()
                return out
            if start:
                del buf[:start]
            depth, in_str, esc = 0, False, False
            end = -1
            for i, ch in enumerate(buf):
                if in_str:
                    if esc:
                        esc = False
                    elif ch == 0x5C:
                        esc = True
                    elif ch == 0x22:
                        in_str = False
                    continue
                if ch == 0x22:
                    in_str = True
                elif ch == 0x7B:
                    depth += 1
                elif ch == 0x7D:
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end < 0:
                return out
            out.append(buf[:end].decode('utf-8', 'replace'))
            del buf[:end]

    def _handle(self, text: str) -> None:
        try:
            obj = json.loads(text)
        except Exception:                          # noqa: BLE001
            self.stats['failed'] += 1
            return
        if not isinstance(obj, dict):
            self.stats['failed'] += 1
            return
        self.stats['parsed'] += 1
        self.on_frame(obj)

    def stop(self) -> None:
        self.stop_event.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.join(timeout=2)


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
class LiveWindow:
    """收帧 → 写库 → 刷新界面。所有控件只在主线程更新。"""

    REFRESH_MS = 500

    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.data_dir, self.data_dir_source = resolve_data_dir(args.data_dir)
        self.pinned = os.path.abspath(args.db) if args.db else ''
        self.grace = float(getattr(args, 'grace', 60.0))
        self.watcher = SessionWatcher(self.data_dir, self.pinned, grace=self.grace)
        self.sink: Optional[DbSink] = None
        self.db_path = ''
        self.link: Optional[EyeLink] = None
        self.frames = 0
        self.written_session = 0
        self.skipped_dup = 0
        self.dropped_no_db = 0
        self.offset: Optional[float] = None
        self.last_dev_ms = 0
        self.logs: 'queue.Queue[str]' = queue.Queue()
        self.started = False
        self._warned_ambiguous = False

        self._build()
        self._tick()

    # ---------------- 界面骨架 ----------------
    def _build(self) -> None:
        self.root.title('眼动实时入库监控')
        style = ttk.Style()
        try:
            style.theme_use('vista')
        except tk.TclError:
            pass

        top = ttk.Frame(self.root, padding=(12, 10, 12, 2))
        top.pack(fill='x')
        self.state_label = ttk.Label(top, text='● 未开始', font=('Microsoft YaHei', 12, 'bold'),
                                     foreground='#888888')
        self.state_label.pack(side='left')
        self.hint = ttk.Label(top, text='写入「当前最新」的会话库', foreground='#777777')
        self.hint.pack(side='right')

        db_row = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        db_row.pack(fill='x')
        self.db_label = ttk.Label(db_row, text='目标库：—', font=('Consolas', 9),
                                  foreground='#333333')
        self.db_label.pack(side='left')

        cards = ttk.Frame(self.root, padding=(10, 4))
        cards.pack(fill='x')
        self.values: Dict[str, ttk.Label] = {}
        specs = (('frames', '收帧', '0'), ('written', '已入库', '0'),
                 ('rate', '帧率', '0.0'), ('size', '库大小', '0 KB'),
                 ('updated', '最后更新', '—'))
        for col, (key, title, init) in enumerate(specs):
            cards.columnconfigure(col, weight=1, uniform='card')
            box = ttk.LabelFrame(cards, text=title, padding=(8, 3))
            box.grid(row=0, column=col, sticky='nsew', padx=4)
            value = ttk.Label(box, text=init, font=('Consolas', 12, 'bold'))
            value.pack(anchor='w')
            self.values[key] = value

        form = ttk.Frame(self.root, padding=(12, 8, 12, 2))
        form.pack(fill='x')
        form.columnconfigure(5, weight=1)
        ttk.Label(form, text='设备').grid(row=0, column=0, sticky='w')
        # 优先级：命令行 > 上次保存 > 默认值。命令行必须能覆盖记忆，否则联调时
        # 改了端口却一直连旧端口，很难看出来。
        saved = load_settings()
        host = self.args.host or str(saved.get('host') or DEFAULT_HOST)
        port = self.args.port or saved.get('port') or DEFAULT_PORT
        self.host_var = tk.StringVar(value=host)
        ttk.Entry(form, textvariable=self.host_var, width=15).grid(row=0, column=1, padx=(4, 10))
        ttk.Label(form, text='端口').grid(row=0, column=2, sticky='w')
        self.port_var = tk.StringVar(value=str(port))
        ttk.Entry(form, textvariable=self.port_var, width=7).grid(row=0, column=3, padx=(4, 10))
        ttk.Label(form, text='数据目录').grid(row=0, column=4, sticky='w')
        self.dir_var = tk.StringVar(value=self.data_dir)
        ttk.Entry(form, textvariable=self.dir_var).grid(row=0, column=5, sticky='ew', padx=4)
        ttk.Button(form, text='浏览…', command=self._browse).grid(row=0, column=6)

        actions = ttk.Frame(self.root, padding=(12, 4))
        actions.pack(fill='x')
        self.start_button = ttk.Button(actions, text='开始接收', command=self.toggle)
        self.start_button.pack(side='left')
        ttk.Button(actions, text='切换库…', command=self._choose_db).pack(side='left', padx=8)
        ttk.Button(actions, text='打开库所在文件夹', command=self._open_dir).pack(side='left')
        self.source_label = ttk.Label(
            actions, text='目录来源：{}'.format(self.data_dir_source), foreground='#777777')
        self.source_label.pack(side='left', padx=12)

        self.log_box = tk.Text(self.root, height=6, wrap='word', font=('Consolas', 9),
                               state='disabled', relief='flat', background='#f7f7f7')
        self.log_box.pack(fill='both', expand=True, padx=12, pady=(4, 10))

        # 按控件实际需求定尺寸：高分屏缩放后字体变大，写死宽度会把最后一张卡挤出窗口。
        # reqwidth 在高 DPI 下不可靠，所以再兜一个下限（150% 缩放实测需要 ~1050）。
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 1080)
        height = max(self.root.winfo_reqheight(), 470)
        self.root.geometry('{}x{}'.format(width, height))
        self.root.minsize(width, height)

    # ---------------- 交互 ----------------
    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title='选择 data 目录（含会话库）',
                                         initialdir=self.dir_var.get() or os.getcwd())
        if not chosen:
            return
        self.dir_var.set(chosen)
        self.data_dir = chosen
        self.data_dir_source = '手动浏览'
        self.source_label.configure(text='目录来源：手动浏览')
        self.watcher = SessionWatcher(self.data_dir, self.pinned, grace=self.grace)
        save_settings(data_dir=chosen)
        self.log('数据目录改为：{}'.format(chosen))

    def _choose_db(self) -> None:
        rows = self.watcher.candidates()
        if not rows:
            messagebox.showinfo('切换库', '这个目录下还没有会话库：\n{}'.format(self.data_dir))
            return
        win = tk.Toplevel(self.root)
        win.title('选择会话库')
        win.transient(self.root)
        ttk.Label(win, text='按最后数据时间排序；正在写的那一个在最上面',
                  padding=(10, 8)).pack(anchor='w')
        box = tk.Listbox(win, width=96, height=min(12, len(rows)), font=('Consolas', 9))
        for path, latest, mtime in rows:
            stamp = (datetime.fromtimestamp(latest).strftime('%m-%d %H:%M:%S') if latest
                     else '无数据')
            box.insert('end', '{}　最后数据 {}　文件 {}'.format(
                os.path.relpath(path, self.data_dir), stamp,
                datetime.fromtimestamp(mtime).strftime('%H:%M:%S')))
        box.selection_set(0)
        box.pack(fill='both', expand=True, padx=10)

        def confirm():
            index = box.curselection()
            if not index:
                return
            path = rows[index[0]][0]
            self.pinned = path
            self.watcher = SessionWatcher(self.data_dir, self.pinned, grace=self.grace)
            save_settings(pinned_db=path, data_dir=self.data_dir)
            self.log('已钉住库：{}'.format(os.path.basename(path)))
            win.destroy()

        bar = ttk.Frame(win, padding=8)
        bar.pack(fill='x')
        ttk.Button(bar, text='就用这个', command=confirm).pack(side='right')
        ttk.Button(bar, text='取消', command=win.destroy).pack(side='right', padx=8)
        ttk.Button(bar, text='恢复自动', command=lambda: (self._unpin(), win.destroy())
                   ).pack(side='left')

    def _unpin(self) -> None:
        self.pinned = ''
        self.watcher = SessionWatcher(self.data_dir, '', grace=self.grace)
        save_settings(pinned_db='')
        self.log('已恢复自动跟随最新库')

    def _open_dir(self) -> None:
        folder = os.path.dirname(self.db_path) if self.db_path else self.data_dir
        try:
            os.startfile(folder)                 # type: ignore[attr-defined]
        except Exception:                         # noqa: BLE001
            self.log('打不开文件夹：{}'.format(folder))

    def toggle(self) -> None:
        if self.started:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        self.started = True
        self.start_button.configure(text='停止')
        self.data_dir = self.dir_var.get().strip() or self.data_dir
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.log('端口不是数字：{}'.format(self.port_var.get()))
            self.started = False
            self.start_button.configure(text='开始接收')
            return
        save_settings(host=host, port=port, data_dir=self.data_dir)
        self.link = EyeLink(host, port, self._on_frame, self.log)
        self.link.start()
        self.log('开始接收 {}:{}；写入 {}'.format(host, port, self.data_dir))

    def stop(self) -> None:
        self.started = False
        self.start_button.configure(text='开始接收')
        if self.link is not None:
            self.link.stop()
            self.link = None
        if self.sink is not None:
            # 先取统计再关：close() 会把缓冲刷进库，之后置空会让界面停在旧数字
            self.sink.close()
            self.written_session = self.sink.written
            self.skipped_dup = self.sink.skipped_dup
            self.sink = None
        self.log('已停止（本会话入库 {} 行，跳过重复 {} 帧）'.format(
            self.written_session, self.skipped_dup))

    # ---------------- 数据通路（工作线程调用，只做数据，不碰控件） ----------------
    def _on_frame(self, obj: Dict[str, object]) -> None:
        self.frames += 1
        try:
            dev_ms = int(float(obj.get('timestamp', 0)))
        except (TypeError, ValueError):
            dev_ms = 0

        now = time.time()
        if dev_ms:
            # 时间轴：设备毫秒 + 缓慢修正的偏移，既平滑又能随本机时钟校正漂移
            sample = now - dev_ms / 1000.0
            if self.offset is None or abs(sample - self.offset) > 5.0:
                self.offset = sample
            else:
                self.offset = self.offset * 0.98 + sample * 0.02
            stamp = dev_ms / 1000.0 + self.offset
        else:
            stamp = now

        sink = self.sink
        if sink is None:
            self.dropped_no_db += 1
            return
        value = self._build_value(obj, dev_ms)
        try:
            sink.add(datetime.fromtimestamp(stamp).strftime(TIME_FORMAT), value)
            sink.maybe_flush()
            self.written_session = sink.written
            self.skipped_dup = sink.skipped_dup
        except sqlite3.Error as exc:
            self.log('写库失败：{}'.format(exc))

    @staticmethod
    def _num(value) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if number == -1 else number

    def _build_value(self, obj: Dict[str, object], dev_ms: int) -> Dict[str, object]:
        """与导入工具同一套口径：原始字段全留 + 归一化键 + src 标记。"""
        gx, gy = self._num(obj.get('gaze2dX')), self._num(obj.get('gaze2dY'))
        left = obj.get('leftPupil', obj.get('leftPupilDiameter'))
        right = obj.get('rightPupil', obj.get('rightPupilDiameter'))
        raw_valid = obj.get('gaze_valid', obj.get('gazeValid'))
        if isinstance(raw_valid, bool):
            valid = raw_valid
        elif raw_valid is None:
            valid = gx is not None and gy is not None
        else:
            valid = str(raw_valid).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'on')
        value = dict(obj)
        value.update({
            'gaze_valid': valid, 'gaze_x': gx, 'gaze_y': gy,
            'left_pupil': self._num(left), 'right_pupil': self._num(right),
            'ts_unix': round(dev_ms / 1000.0, 6) if dev_ms else None,
            'ts_raw': str(dev_ms) if dev_ms else '',
            'src': 'eye-live',
        })
        return value

    # ---------------- 定时刷新 ----------------
    @staticmethod
    def _short_path(path: str, limit: int = 70) -> str:
        if len(path) <= limit:
            return path
        tail = os.sep + os.sep.join(path.split(os.sep)[-3:])
        return '…' + tail if len(tail) <= limit else '…' + path[-limit:]

    def _tick(self) -> None:
        try:
            self._refresh_db()
            self._refresh_labels()
            self._drain_logs()
        finally:
            self.root.after(self.REFRESH_MS, self._tick)

    def _refresh_db(self) -> None:
        path = self.watcher.poll()
        # 只有「这个库最近真的在收数据」才往里写：选到昨天/上午那次会话的库时，
        # 不能让眼动悄悄写进去 —— 那些数据事后没法区分，只能靠 src 标记捞出来。
        write_ok = bool(path) and (self.watcher.live or bool(self.pinned))

        if not write_ok:
            if self.sink is not None:
                self.sink.close()
                self.written_session = self.sink.written
                self.skipped_dup = self.sink.skipped_dup
                self.sink = None
                self.log('暂停写入：{} 最近没有新数据，可能不是本次采集的库'
                         .format(os.path.basename(path) if path else '（没有库）'))
            self.db_path = path
            return

        if path != self.db_path or self.sink is None:
            if self.sink is not None:
                self.sink.close()
                self.log('切换目标库 → {}（上一库入库 {} 行）'.format(
                    os.path.basename(path), self.written_session))
            try:
                self.sink = DbSink(path)
            except sqlite3.Error as exc:
                self.sink = None
                self.log('打不开库 {}：{}'.format(os.path.basename(path), exc))
                return
            self.db_path = path
            self.written_session = 0
            self.skipped_dup = 0
            self.offset = None
            self.log('锁定目标库：{}'.format(path))

        if self.watcher.ambiguous and not self._warned_ambiguous:
            self._warned_ambiguous = True
            self.log('检测到 {} 个库都在写，已按最新数据选中；点「切换库…」可改成指定的那个'
                     .format(len(self.watcher.ambiguous)))

    def _refresh_labels(self) -> None:
        link = self.link
        connected = bool(link and link.stats['connected'])
        growing = self.watcher.growing
        latest = self.watcher.current_max
        age = (time.time() - latest) if latest else 999.0

        if not self.started:
            text, color = '● 已停止', '#888888'
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
        self.state_label.configure(text=text, foreground=color)

        self.hint.configure(text='重复帧已跳过 {}｜写入「当前最新」的会话库'.format(self.skipped_dup))

        if self.db_path:
            if growing:
                state = '主程序正在写'
            elif age < 600:
                state = '主程序 {} 秒前停写'.format(int(age))
            else:
                state = '这个库最近没数据'
            shown = '{}　（{}）'.format(self._short_path(self.db_path), state)
        else:
            shown = '—（{} 下没找到会话库）'.format(self.data_dir)
        self.db_label.configure(text='目标库：' + shown)
        if self.pinned:
            self.source_label.configure(text='目录来源：{}｜已钉住指定库'.format(self.data_dir_source))
        else:
            self.source_label.configure(text='目录来源：{}'.format(self.data_dir_source))

        self.values['frames'].configure(text=str(self.frames))
        self.values['written'].configure(text=str(self.written_session))
        elapsed = max(time.time() - (link.stats['started_at'] if link else time.time()), 0.001)
        self.values['rate'].configure(
            text='{:.1f}'.format(link.stats['parsed'] / elapsed) if link else '0.0')
        size = self.sink.size if self.sink else 0
        self.values['size'].configure(
            text='{:.0f} KB'.format(size / 1024) if size < 1024 * 1024
            else '{:.1f} MB'.format(size / 1024 / 1024))
        stamp = link.stats['last_at'] if link else 0
        self.values['updated'].configure(
            text=datetime.fromtimestamp(stamp).strftime('%H:%M:%S') if stamp else '—')

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


def _install_error_log() -> None:
    """打包成无控制台的 exe 后，异常不能只留在看不见的 stderr 里。"""
    import traceback

    def hook(exc_type, exc, tb):
        text = ''.join(traceback.format_exception(exc_type, exc, tb))
        path = os.path.join(os.path.dirname(SETTINGS_PATH), 'eye_gui_error.log')
        try:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write('==== {}\n{}'.format(datetime.now().strftime(TIME_FORMAT), text))
        except OSError:
            pass
        try:
            from tkinter import messagebox
            messagebox.showerror('眼动界面出错',
                                 (text[-600:] + '\n\n完整日志：' + path) if text else '未知错误')
        except Exception:                                     # noqa: BLE001
            pass

    sys.excepthook = hook


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='眼动实时入库监控（不依赖主程序）')
    ap.add_argument('--host', default=None, help='设备地址，默认 192.168.2.98（也可用记住的值）')
    ap.add_argument('-p', '--port', type=int, default=None,
                    help='设备端口，默认 7901（也可用记住的值）')
    ap.add_argument('--db', default='', help='钉死写某个库；不给就自动跟随正在写的那个')
    ap.add_argument('--data-dir', default='', help='会话库所在目录；不给就自动探测')
    ap.add_argument('--grace', type=float, default=60.0,
                    help='最后一条数据距今多少秒内才认为「这次采集还在进行」，默认 60')
    ap.add_argument('--selftest', action='store_true', help='无窗口自检（4 秒后退出）')
    args = ap.parse_args(argv)

    _enable_dpi_awareness()
    _install_error_log()
    root = tk.Tk()
    app = LiveWindow(root, args)
    if args.selftest:
        app.start()
        deadline = time.time() + 4.0
        while time.time() < deadline:
            root.update()
            time.sleep(0.05)
        app.stop()
        print('自检：目录={}（{}）｜收帧 {}｜入库 {}｜跳过重复 {}｜无库丢弃 {}｜库 {}'.format(
            app.data_dir, app.data_dir_source, app.frames, app.written_session,
            app.skipped_dup, app.dropped_no_db, os.path.basename(app.db_path) or '无'))
        root.destroy()
        return 0 if app.written_session > 0 else 1

    app.start()
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
