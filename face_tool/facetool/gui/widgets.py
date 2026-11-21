# -*- coding: utf-8 -*-
"""GUI 通用小部件：目录选择、统计卡片、日志面板。"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable, Dict, Optional


class DirPicker(ttk.Frame):
    """一行：标签 + 输入框 + 浏览按钮。"""

    def __init__(self, master, label: str, width: int = 78,
                 on_change: Optional[Callable[[str], None]] = None):
        super().__init__(master)
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text=label).grid(row=0, column=0, sticky='w')
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var)
        self.entry.grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(self, text='浏览…', width=7, command=self._browse).grid(row=0, column=2)
        self._on_change = on_change
        if on_change is not None:
            self.var.trace_add('write', lambda *_a: on_change(self.var.get()))

    def _browse(self) -> None:
        initial = self.var.get() or os.getcwd()
        if not os.path.isdir(initial):
            initial = os.path.dirname(initial) or os.getcwd()
        chosen = filedialog.askdirectory(title='选择目录', initialdir=initial)
        if chosen:
            self.var.set(os.path.normpath(chosen))

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value or '')


class StatCards(ttk.Frame):
    """一排指标卡：标签在上、数值在下。"""

    def __init__(self, master, keys, columns: int = 5):
        super().__init__(master)
        self.values: Dict[str, ttk.Label] = {}
        for col, (key, title) in enumerate(keys):
            self.columnconfigure(col, weight=1, uniform='card')
            box = ttk.LabelFrame(self, text=title, padding=(8, 2))
            box.grid(row=0, column=col, sticky='nsew', padx=3, pady=2)
            label = ttk.Label(box, text='—', font=('Consolas', 12, 'bold'))
            label.pack(anchor='w')
            self.values[key] = label

    def set(self, key: str, text: str) -> None:
        label = self.values.get(key)
        if label is not None and label.cget('text') != text:
            label.configure(text=text)


class LogPane(ttk.Frame):
    """只读日志，最多保留 max_lines 行。"""

    def __init__(self, master, height: int = 8, max_lines: int = 400):
        super().__init__(master)
        self.max_lines = max_lines
        self.text = tk.Text(self, height=height, wrap='word', font=('Consolas', 9),
                            state='disabled', relief='flat', background='#fafafa')
        scroll = ttk.Scrollbar(self, orient='vertical', command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

    def append(self, line: str) -> None:
        self.text.configure(state='normal')
        self.text.insert('end', line.rstrip() + '\n')
        total = int(self.text.index('end-1c').split('.')[0])
        if total > self.max_lines:
            self.text.delete('1.0', '{}.0'.format(total - self.max_lines))
        self.text.see('end')
        self.text.configure(state='disabled')

    def clear(self) -> None:
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')
