#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""心电贴单机检测工具（独立运行，不依赖主程序，不改动任何采集代码）。

用途：把一片心电贴连上，实时看它到底有没有在工作——
      电极贴没贴好、波形是不是真的心电、丢包多少、电量多少。

用法：
    python tools/ble/ecg_probe_gui.py
    python tools/ble/ecg_probe_gui.py --auto E1220511000212     # 启动即连指定设备
    python tools/ble/ecg_probe_gui.py --device E1220511000212   # 只预选，不自动连

界面分区：
  上   设备选择 / 扫描 / 连接 / 断开
  中左 连接状态与阶段（扫描→建链→服务发现→特征→握手→订阅）
  中右 实时指标：包速率、采样率、丢包、体温、波形判断、MTU
  下   滚动心电波形（最近 6 秒）+ 日志

判读要点：
  · 刚贴上时前几十秒是放大器基线恢复期，峰值顶到 3 万以上属正常现象
  · 「波形判断」依据峰峰值与体温：>2 万判饱和、<200 判过弱、体温 <32℃ 判疑似未贴身
  · offset 10 那个字节在实测中贴身前后恒为 0x80，不能当电极脱落标志，仅作记录
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import configparser
import math
import os
import queue
import random
import struct
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from bleak import BleakClient, BleakScanner

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SERVICE_UUID = '554b0001-15e1-4af1-a6e3-588089f20b68'
PACKET_SIZE = 244
HEADER, TAIL = 0x5B, 0x5D

CMD_END_COLLECT = bytearray([0x5B, 0xC1, 0x00, 0x00, 0x23, 0xFE, 0x5D])
CMD_START_COLLECT = bytearray([0x5B, 0xC0, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x41, 0x5C, 0x5D])

BATTERY_UUID = '00002a19-0000-1000-8000-00805f9b34fb'
FW_UUID = '00002a26-0000-1000-8000-00805f9b34fb'
HW_UUID = '00002a27-0000-1000-8000-00805f9b34fb'
VENDOR_UUID = '00002a29-0000-1000-8000-00805f9b34fb'


# ---------------------------------------------------------------------------
# 协议解码（与主程序 offsets 一致，另加 loff 与 1 字节包号）
# ---------------------------------------------------------------------------
def decode(pkt: bytes):
    if len(pkt) != PACKET_SIZE or pkt[0] != HEADER or pkt[-1] != TAIL:
        return None
    out = {
        'seq': struct.unpack_from('<H', pkt, 4)[0],   # 包号是 uint16 小端（offset 4~5）
        'cmd': pkt[1],
        'loff': pkt[10],
        'temp': struct.unpack_from('<h', pkt, 11)[0] / 100.0,
        'type1': pkt[8], 'type2': pkt[173], 'type3': pkt[191],
        'ecg': list(struct.unpack_from('<80h', pkt, 13)),
        'crc': pkt[241:243].hex().upper(),
    }
    if pkt[173] == 0x03:
        out['resp'] = list(struct.unpack_from('<8h', pkt, 175))
    if pkt[191] == 0x02:
        n = pkt[192] // 3
        try:
            ax = struct.unpack_from('<%db' % n, pkt, 193)
            ay = struct.unpack_from('<%db' % n, pkt, 209)
            az = struct.unpack_from('<%db' % n, pkt, 225)
            out['acc'] = [(ax[i] / 64.0, ay[i] / 64.0, az[i] / 64.0) for i in range(n)]
        except struct.error:
            pass
    return out


class Assembler:
    """把分片通知重组成 244 字节整包（MTU 不够时一条包会拆成多条通知）。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk: bytes):
        self.buf.extend(chunk)
        packets = []
        while len(self.buf) >= PACKET_SIZE:
            if self.buf[0] == HEADER and self.buf[PACKET_SIZE - 1] == TAIL:
                packets.append(bytes(self.buf[:PACKET_SIZE]))
                del self.buf[:PACKET_SIZE]
                continue
            idx = self.buf.find(bytes([HEADER]), 1 if self.buf[0] == HEADER else 0)
            if idx == -1:
                self.buf.clear()
            else:
                del self.buf[:idx]
        return packets


# ---------------------------------------------------------------------------
# BLE 工作线程：独立事件循环，界面只通过队列拿数据
# ---------------------------------------------------------------------------
class BleWorker(threading.Thread):
    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.events = events
        self.loop = asyncio.new_event_loop()
        self.client = None
        self._task = None
        self._stop = False
        self._asm = Assembler()
        self._notify_char = None

    # -- 生命周期 -----------------------------------------------------------
    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def post(self, kind, **kw):
        self.events.put((kind, kw))

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def shutdown(self):
        self._stop = True
        if self._task is not None:
            self.loop.call_soon_threadsafe(self._task.cancel)

    # -- 操作 ---------------------------------------------------------------
    def start_connect(self, device):
        self._task = self.submit(self._connect(device))

    def disconnect(self):
        self._stop = True
        if self._task is not None:
            self.loop.call_soon_threadsafe(self._task.cancel)

    async def _stage(self, text):
        self.post('stage', text=text)

    async def _connect(self, device):
        self._stop = False
        client = BleakClient(device, timeout=12.0)
        self.client = client
        try:
            await self._stage('建立连接…')
            t0 = time.perf_counter()
            try:
                await client.connect()
            except Exception as exc:                      # noqa: BLE001
                self.post('failed', text='{}: {}'.format(type(exc).__name__, exc))
                return
            self.post('info', text='已建链，用时 {:.2f}s'.format(time.perf_counter() - t0))

            await self._stage('服务发现…')
            t0 = time.perf_counter()
            services = list(client.services)
            self.post('info', text='服务发现完成，{} 个服务，{:.2f}s'.format(
                len(services), time.perf_counter() - t0))

            await self._stage('读取设备信息…')
            await self._read_info(client)

            await self._stage('定位特征…')
            notify_char, cmd_char = self._pick_chars(services)
            if notify_char is None:
                self.post('failed', text='目标服务内没有 notify 特征')
                return
            if cmd_char is None:
                self.post('failed', text='目标服务内没有可写特征，无法下发开始命令')
                return
            self.post('info', text='通知 0x{:04X} ／ 命令 0x{:04X}'.format(
                notify_char.handle, cmd_char.handle))

            await self._stage('下发开始采集…')
            for label, cmd in (('结束采集', CMD_END_COLLECT), ('开始采集', CMD_START_COLLECT)):
                try:
                    await asyncio.wait_for(
                        client.write_gatt_char(cmd_char, cmd, response=True), timeout=8)
                    self.post('info', text='{} 命令已下发'.format(label))
                    await asyncio.sleep(0.1)
                except asyncio.TimeoutError:
                    self.post('failed', text='{} 命令 8 秒无 ATT 响应'.format(label))
                    return
                except Exception as exc:                  # noqa: BLE001
                    self.post('info', text='{} 命令失败：{}'.format(label, exc))

            await self._stage('订阅通知…')
            self._asm = Assembler()
            try:
                await asyncio.wait_for(client.start_notify(notify_char, self._on_notify),
                                       timeout=8)
            except asyncio.TimeoutError:
                self.post('failed', text='订阅超时（写 CCCD 无响应）')
                return
            self.post('connected', mtu=client.mtu_size)

            while client.is_connected:
                await asyncio.sleep(0.5)
            self.post('failed', text='链路已断开')
        except asyncio.CancelledError:
            self.post('info', text='已手动断开')
        except Exception as exc:                          # noqa: BLE001
            self.post('failed', text='{}: {}'.format(type(exc).__name__, exc))
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:                             # noqa: BLE001
                pass

    async def _read_info(self, client):
        for label, uuid in (('电量', BATTERY_UUID), ('厂商', VENDOR_UUID),
                            ('硬件', HW_UUID), ('固件', FW_UUID)):
            try:
                raw = await asyncio.wait_for(client.read_gatt_char(uuid), timeout=4)
                text = raw.decode('utf-8', 'replace').strip('\x00') if len(raw) > 1 else str(raw[0])
                self.post('info', text='{}：{}'.format(label, text))
                if label == '电量':
                    self.post('battery', value=text)
            except Exception:                             # noqa: BLE001
                pass

    def _pick_chars(self, services):
        notify_char = cmd_char = None
        for svc in services:
            if svc.uuid.lower() != SERVICE_UUID:
                continue
            for ch in svc.characteristics:
                if notify_char is None and 'notify' in ch.properties:
                    notify_char = ch
                if cmd_char is None and ('write' in ch.properties
                                         or 'write-without-response' in ch.properties):
                    cmd_char = ch
        if notify_char is None:
            for svc in services:
                for ch in svc.characteristics:
                    if 'notify' in ch.properties:
                        notify_char = ch
                        break
                if notify_char:
                    break
        return notify_char, cmd_char

    def _on_notify(self, _sender, data):
        for pkt in self._asm.feed(bytes(data)):
            decoded = decode(pkt)
            if decoded is not None:
                self.post('packet', data=decoded, raw=pkt.hex().upper())


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
class App:
    REFRESH_MS = 120

    def __init__(self, root: tk.Tk, auto_name: str = '', preselect: str = '',
                 demo: bool = False, repeat: int = 0):
        self.root = root
        self.events = queue.Queue()
        self.worker = BleWorker(self.events)
        self.worker.start()

        self.samples = collections.deque(maxlen=1500)
        self.all_samples = collections.deque(maxlen=60000)
        self.packets = 0
        self.lost = 0
        self._last_seq = None
        self._pkt_times = collections.deque(maxlen=40)
        self._connected = False
        self._loff = None
        self._temp = None
        self._mtu = None
        self._last_raw = ''
        self._first_pkt = None
        self.peak = 0
        self._retry = 0
        self._reconnects = 0
        self._manual_stop = False
        self._last_dev = None
        self._drops = 0
        self._backoff = 2.0
        self._attempt = 0
        self._connected_at = 0.0
        # 会话级累计（跨重连不清零）：工具要能看出「先丢包、后掉线」的整个过程
        self.sess_packets = 0
        self.sess_lost = 0
        self.sess_attempts = 0
        self.sess_started = 0.0
        self.history = []
        self.max_attempts = max(0, repeat)     # >0 时连满 N 次就停并出汇总
        # 诊断观测点：区分「建链失败 / 建链无数据 / 数据中断后超时」
        self._attempt_t0 = 0.0
        self._conn_ms = None
        self._first_pkt_ms = None
        self._last_notify = 0.0
        self._mtu_cur = None
        self._last_seq_seen = None

        self.devices = {}
        self._build()
        self._poll()
        if preselect:
            self.dev_var.set(preselect)
        if auto_name:
            self.root.after(400, lambda: self._scan_then_connect(auto_name))
        if demo:
            self.root.after(300, self._start_demo)

    # -- 合成数据（只用于验证界面，不连设备）--------------------------------
    def _start_demo(self):
        self._append('演示模式：合成心电数据，仅用于检查界面显示')
        self._set_stage('演示模式（合成数据，非真实设备）', '#7a5b00', '#fff8e1')
        self._connected = True
        self.sess_started = time.time()
        self.sess_attempts = 1
        self._mtu = 247
        self._temp = 35.42
        self._loff = 0
        threading.Thread(target=self._demo_loop, daemon=True).start()

    def _demo_loop(self):
        seq = 0
        n = 0
        period = 0.36                                   # 每次 80 点 ≈ 0.32 秒
        while True:
            time.sleep(period)
            ecg = []
            for i in range(80):
                t = (n + i) / 250.0                      # 250 Hz
                phase = (t * 1.2) % 1.0                  # 72 bpm
                v = int(220 * math.sin(2 * math.pi * 0.25 * t))
                if phase < 0.05:                         # QRS 尖峰
                    v += int(2600 * math.sin(math.pi * phase / 0.05))
                elif phase < 0.22:                       # T 波
                    v += int(700 * math.sin(math.pi * (phase - 0.05) / 0.17))
                ecg.append(max(-32000, min(32000, v)))
            n += 80
            seq = (seq + 1) & 0xFF
            self.events.put(('packet', {
                'data': {'seq': seq, 'cmd': 0xB2, 'loff': 0, 'temp': 35.42,
                         'type1': 1, 'type2': 3, 'type3': 2, 'ecg': ecg,
                         'crc': '0000'},
                'raw': 'DEMO'}))
        # 线程不退出，随进程结束回收

    # -- 布局 ---------------------------------------------------------------
    def _build(self):
        self.root.title('心电贴检测工具')
        self.root.geometry('1120x740')
        self.root.minsize(940, 640)

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill='x')
        ttk.Label(top, text='设备').pack(side='left')
        self.dev_var = tk.StringVar()
        self.cmb = ttk.Combobox(top, textvariable=self.dev_var, width=42,
                                values=self._configured_names())
        self.cmb.pack(side='left', padx=6)
        self.btn_scan = ttk.Button(top, text='扫描', command=self._scan)
        self.btn_scan.pack(side='left', padx=3)
        self.btn_conn = ttk.Button(top, text='连接', command=self._connect)
        self.btn_conn.pack(side='left', padx=3)
        self.btn_disc = ttk.Button(top, text='断开', command=self._disconnect, state='disabled')
        self.btn_disc.pack(side='left', padx=3)
        ttk.Button(top, text='复制诊断信息', command=self._copy).pack(side='right')

        state = ttk.Frame(self.root, padding=(10, 0))
        state.pack(fill='x')
        self.stage = tk.StringVar(value='未连接')
        self.stage_lbl = tk.Label(state, textvariable=self.stage, anchor='w',
                                  font=('Microsoft YaHei UI', 11, 'bold'),
                                  fg='#7a5b00', bg='#fff8e1', padx=8, pady=6)
        self.stage_lbl.pack(fill='x')

        body = ttk.Frame(self.root, padding=(10, 8))
        body.pack(fill='x')

        left = ttk.LabelFrame(body, text='实时指标', padding=8)
        left.pack(side='left', fill='both', expand=True)
        self.vals = {}
        rows = [('rate', '包速率'), ('sr', '采样率'), ('packets', '累计包数'),
                ('lost', '丢包'), ('signal', '波形判断'), ('temp', '体温'),
                ('peak', '峰峰值'), ('battery', '电量'), ('mtu', 'MTU'),
                ('attempts', '连接次数'), ('total', '累计丢包'), ('elapsed', '会话时长')]
        for i, (key, label) in enumerate(rows):
            r, c = divmod(i, 3)
            ttk.Label(left, text=label, foreground='#666666').grid(
                row=r, column=c * 2, sticky='w', padx=(0, 4), pady=2)
            value = tk.StringVar(value='—')
            lbl = tk.Label(left, textvariable=value, anchor='w',
                           font=('Consolas', 11, 'bold'), width=12)
            lbl.grid(row=r, column=c * 2 + 1, sticky='w', padx=(0, 18), pady=2)
            self.vals[key] = (value, lbl)

        wave_box = ttk.LabelFrame(self.root, text='心电波形（最近约 6 秒）', padding=6)
        wave_box.pack(fill='both', expand=True, padx=10, pady=(0, 6))
        self.canvas = tk.Canvas(wave_box, height=190, bg='#ffffff',
                                highlightthickness=1, highlightbackground='#d8d8d8')
        self.canvas.pack(fill='both', expand=True)

        log_box = ttk.LabelFrame(self.root, text='日志', padding=6)
        log_box.pack(fill='both', expand=False, padx=10, pady=(0, 10))
        self.log = tk.Text(log_box, height=9, wrap='word', font=('Consolas', 9),
                           bg='#fbfbfb', relief='flat')
        self.log.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(log_box, command=self.log.yview)
        sb.pack(side='right', fill='y')
        self.log.configure(yscrollcommand=sb.set)

        self._append('提示：先点「扫描」选中设备，再点「连接」。刚贴上时前几十秒是基线恢复期，峰值顶到 3 万属正常。')

    def _configured_names(self):
        parser = configparser.ConfigParser()
        parser.read(os.path.join(ROOT, 'config.ini'), encoding='utf-8')
        raw = parser.get('bluetooth', 'device_names', fallback='')
        return [n.strip() for n in raw.split(',') if n.strip()]

    # -- 动作 ---------------------------------------------------------------
    def _scan(self, then_connect: str = ''):
        self._set_stage('扫描中…（10 秒）', '#7a5b00', '#fff8e1')
        self._append('开始扫描广播')

        def work():
            found = asyncio.run(BleakScanner.discover(timeout=10.0, return_adv=True))
            hits = []
            for dev, adv in found.values():
                name = dev.name or getattr(adv, 'local_name', '') or ''
                if name:
                    hits.append((name, dev.address, adv.rssi, dev))
            hits.sort(key=lambda x: -x[2])
            self.events.put(('scan_done', {'hits': hits, 'total': len(found),
                                           'then': then_connect}))

        threading.Thread(target=work, daemon=True).start()

    def _scan_then_connect(self, name: str):
        self._pending_auto = name
        self._scan(then_connect=name)

    def _connect(self):
        name = self.dev_var.get().strip()
        if not name:
            messagebox.showinfo('连接', '先扫描并选择一个设备')
            return
        dev = self.devices.get(name)
        if dev is None:
            messagebox.showinfo('连接', '这个设备还没扫描到，先点「扫描」')
            return
        self._begin(dev)

    def _begin(self, dev):
        """开始一次连接尝试（手动连接与自动重连共用）。

        注意：只重置「本次尝试」的计数，会话累计（sess_*）与 history 保留，
        否则每次重连都会把前面的丢包抹掉，看不到「先丢包后掉线」的过程。
        """
        self.sess_attempts += 1
        if not self.sess_started:
            self.sess_started = time.time()
        self._attempt_t0 = time.time()
        self._conn_ms = None
        self._first_pkt_ms = None
        self._last_notify = 0.0
        self.packets = 0
        self.lost = 0
        self._last_seq = None
        self._pkt_times.clear()
        self.samples.clear()
        self.all_samples.clear()
        self._first_pkt = None
        self.peak = 0
        self._set_stage('连接中…', '#7a5b00', '#fff8e1')
        self.btn_conn.configure(state='disabled')
        self.btn_disc.configure(state='normal')
        self._manual_stop = False
        self._last_dev = dev
        self.worker.start_connect(dev)

    def _auto_reconnect(self):
        if self._manual_stop or self._last_dev is None:
            return
        if self.max_attempts and self.sess_attempts >= self.max_attempts:
            self._finish_batch()
            return
        self._reconnects += 1
        self._append('自动重连（第 {} 次）'.format(self._reconnects))
        self._begin(self._last_dev)

    def _finish_batch(self):
        """连满 N 次后出汇总：成功率、平均持续时长、平均丢包率。

        「偶尔能收」这种临界状态，只有把成功率量化出来，才能判断某次
        改动（换口、拉远、改设置）是否真的有效。
        """
        attempts = max(1, self.sess_attempts)
        ok = [h for h in self.history if h['packets'] > 0]
        held = [h['held'] for h in ok]
        rate = [h['rate'] for h in ok]
        lines = [
            '──────── 批量结果（{} 次尝试）────────'.format(attempts),
            '成功建立并收到数据：{} 次（{:.0f}%）'.format(len(ok), 100.0 * len(ok) / attempts),
            '平均持续：{:.1f} 秒'.format(sum(held) / len(held) if held else 0.0),
            '成功连接的平均丢包率：{:.1f}%'.format(sum(rate) / len(rate) if rate else 0.0),
        ]
        for h in self.history:
            lines.append('  第{}次 {:.1f}s 收{} 丢{}（{:.1f}%）'.format(
                h['n'], h['held'], h['packets'], h['lost'], h['rate']))
        text = '\n'.join(lines)
        self._append(text)
        print(text, flush=True)
        self._set_stage('批量完成：{}/{} 次成功'.format(len(ok), attempts),
                        '#7a5b00', '#fff8e1')

    def _next_backoff(self) -> float:
        """2 → 4 → 8 → 16 → 30 秒，带 ±15% 抖动。稳定连接 10 秒后由 _poll 归零。"""
        base = self._backoff
        self._attempt += 1
        self._backoff = min(30.0, base * 2)
        jittered = base * random.uniform(0.85, 1.15)
        return round(min(30.0, max(1.0, jittered)), 1)

    def _disconnect(self):
        self._manual_stop = True
        self.worker.disconnect()
        self._connected = False
        self._set_stage('已断开', '#555555', '#f2f2f2')
        self.btn_conn.configure(state='normal')
        self.btn_disc.configure(state='disabled')

    def _copy(self):
        total = self.packets + self.lost
        rate = (100.0 * self.lost / total) if total else 0.0
        info = [
            '设备：{}'.format(self.dev_var.get()),
            '阶段：{}'.format(self.stage.get()),
            '包数：{}  丢包：{} ({:.1f}%)  峰峰值：{}'.format(self.packets, self.lost, rate, self.peak),
            '状态字节：{}  体温：{}  MTU：{}'.format(self._loff, self._temp, self._mtu),
            '连接次数：{}  自行掉线：{} 次  自动重连：{} 次'.format(
                self.sess_attempts, self._drops, self._reconnects),
            '会话累计：收 {} 包、丢 {} 包（{:.1f}%）'.format(
                self.sess_packets, self.sess_lost,
                100.0 * self.sess_lost / (self.sess_packets + self.sess_lost)
                if (self.sess_packets + self.sess_lost) else 0.0),
            '首包：{}'.format(self._first_pkt or '（尚未收到）'),
            '末包：{}'.format(self._last_raw),
        ]
        if self.history:
            info.append('各次掉线明细：')
            for h in self.history[-8:]:
                info.append('  第{2}次 持续 {0:.1f}s 收 {1} 丢 {3}（{4:.1f}%）'.format(
                    h['held'], h['packets'], h['n'], h['lost'], h['rate']))
        self.root.clipboard_clear()
        self.root.clipboard_append('\n'.join(info))
        self._append('诊断信息已复制到剪贴板')

    # -- 事件循环 -----------------------------------------------------------
    def _poll(self):
        dirty = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                dirty = True
                if kind == 'stage':
                    self._set_stage(payload['text'], '#7a5b00', '#fff8e1')
                    self._append('· ' + payload['text'])
                elif kind == 'info':
                    self._append(payload['text'])
                elif kind == 'failed':
                    text = payload['text']
                    self._connected = False
                    self.btn_disc.configure(state='disabled')
                    if text == '链路已断开':
                        self._drops += 1
                        self.sess_packets += self.packets
                        self.sess_lost += self.lost
                        held = (time.time() - self._connected_at) if self._connected_at else 0.0
                        total = self.packets + self.lost
                        rate = (100.0 * self.lost / total) if total else 0.0
                        slow = '  ← 丢包偏高，断开前链路已在劣化' if rate >= 5 else ''
                        self._append('[掉线] 第 {} 次自行断开：持续 {:.1f} 秒，收 {} 包、丢 {}（{:.1f}%）{}'.format(
                            self._drops, held, self.packets, self.lost, rate, slow))
                        self.history.append({
                            'n': self._drops, 'held': held, 'packets': self.packets,
                            'lost': self.lost, 'rate': rate})
                        s_total = self.sess_packets + self.sess_lost
                        if s_total:
                            self._append('        会话累计：收 {}、丢 {}（{:.1f}%）'.format(
                                self.sess_packets, self.sess_lost,
                                100.0 * self.sess_lost / s_total))

                    if not self._manual_stop and self._last_dev is not None:
                        # 指数退避：首次重试短（设备刚断开通常很快重新广播），
                        # 连续失败则逐步拉长到 30 秒，避免重连风暴把连接表占满。
                        if text != '链路已断开':
                            # 连接失败通常意味着链路还没释放（supervision timeout 4~6 秒），
                            # 起点直接抬到 4 秒，别在占用窗口里空打。
                            self._backoff = max(self._backoff, 4.0)
                        delay = self._next_backoff()
                        self._append('  {} 秒后重连（退避第 {} 档，上限 30 秒）'.format(
                            delay, self._attempt))
                        self._set_stage('{}，{} 秒后重连'.format(text, delay),
                                        '#7a5b00', '#fff8e1')
                        self.root.after(int(delay * 1000), self._auto_reconnect)
                    else:
                        self._set_stage('失败：' + text, '#8b1a1a', '#fdecec')
                        self._append('[失败] ' + text)
                        self.btn_conn.configure(state='normal')
                elif kind == 'connected':
                    self._connected = True
                    self._connected_at = time.time()
                    self._mtu = payload.get('mtu')
                    self._mtu_cur = payload.get('mtu')
                    self._conn_ms = int((self._connected_at - self._attempt_t0) * 1000)
                    self._set_stage('已连接，正在接收数据', '#146c43', '#e8f5ec')
                    self._append('已连接，MTU={}'.format(self._mtu))
                elif kind == 'battery':
                    self.vals['battery'][0].set(str(payload.get('value')))
                elif kind == 'scan_done':
                    self._on_scan_done(payload)
                elif kind == 'packet':
                    self._on_packet(payload['data'], payload['raw'])
        except queue.Empty:
            pass

        # 稳定连接 10 秒才把退避计数归零，避免「连上就断」把退避反复打回起点
        if (self._connected and self._connected_at
                and time.time() - self._connected_at > 10 and self._attempt):
            self._backoff = 2.0
            self._attempt = 0
            self._append('已稳定连接 10 秒，退避计数归零')

        if dirty:
            self._refresh_stats()
        self._draw()
        self.root.after(self.REFRESH_MS, self._poll)

    def _on_scan_done(self, payload):
        hits = payload['hits']
        self.devices = {name: dev for name, _addr, _rssi, dev in hits}
        names = [n for n, _a, _r, _d in hits]
        self.cmb.configure(values=names or self._configured_names())
        self._append('扫描完成：共 {} 个广播，{} 个带名字'.format(payload['total'], len(names)))
        for name, addr, rssi, _dev in hits[:12]:
            self._append('    {:<22} {:<18} {} dBm'.format(name, addr, rssi))

        target = payload.get('then') or getattr(self, '_pending_auto', '')
        if target and target in self.devices:
            self._retry = 0
            self.dev_var.set(target)
            self._pending_auto = ''
            self._append('自动连接：' + target)
            self._connect()
        elif target:
            self._retry += 1
            self._set_stage('未找到 {}，第 {} 次重试中（请给贴片上电）'.format(target, self._retry),
                            '#7a5b00', '#fff8e1')
            self._append('[未找到] {}（第 {} 次），5 秒后重扫'.format(target, self._retry))
            self._pending_auto = target
            self.root.after(5000, lambda: self._scan(then_connect=target))
        else:
            self._set_stage('扫描完成，请选择设备后点「连接」', '#555555', '#f2f2f2')

    def _on_packet(self, d, raw):
        self.packets += 1
        if self._first_pkt is None:
            self._first_pkt = raw
            self._append('首包：' + raw[:64] + ' …')
        self._last_raw = raw
        now = time.time()
        if self._first_pkt_ms is None and self._attempt_t0:
            self._first_pkt_ms = int((now - self._attempt_t0) * 1000)
            self._append('  首包到达：建链后 {} ms'.format(max(0, self._first_pkt_ms - (self._conn_ms or 0))))
        self._last_notify = now
        self._loff = d['loff']
        self._temp = d['temp']
        if self._last_seq is not None:
            step = (d['seq'] - self._last_seq) & 0xFFFF
            if step == 0:
                self.lost += 0
            elif step > 1:
                self.lost += step - 1
        self._last_seq = d['seq']
        self.samples.extend(d['ecg'])
        self.all_samples.extend(d['ecg'])
        recent = list(self.all_samples)[-1250:]
        self.peak = max(recent) - min(recent) if recent else 0
        self._pkt_times.append(time.time())

    def _refresh_stats(self):
        now = time.time()
        while self._pkt_times and now - self._pkt_times[0] > 2.0:
            self._pkt_times.popleft()
        rate = len(self._pkt_times) / 2.0 if self._pkt_times else 0.0
        self.vals['rate'][0].set('{:.1f} 包/秒'.format(rate))
        self.vals['sr'][0].set('{:.0f} Hz'.format(rate * 80))
        self.vals['packets'][0].set(str(self.packets))
        total = self.packets + self.lost
        if total:
            self.vals['lost'][0].set('{} ({:.1f}%)'.format(self.lost, 100.0 * self.lost / total))
        else:
            self.vals['lost'][0].set('0')
        # 波形判断：只看峰值区间与体温。offset 10 在实测里恒为 0x80（贴身前后都一样），
        # 不能当电极脱落标志用，所以这里不拿它做判据。
        if self.packets < 3:
            text, color = '—', '#333333'
        elif self.peak > 20000:
            text, color = '饱和/未贴身', '#8b1a1a'
        elif self.peak < 200:
            text, color = '信号过弱', '#8a5a00'
        elif self._temp is not None and self._temp < 32:
            text, color = '疑似未贴身', '#8a5a00'
        else:
            text, color = '有信号', '#146c43'
        self.vals['signal'][0].set(text)
        self.vals['signal'][1].configure(fg=color)
        self.vals['temp'][0].set('—' if self._temp is None else '{:.2f} ℃'.format(self._temp))
        self.vals['attempts'][0].set(str(self.sess_attempts))
        s_total = self.sess_packets + self.sess_lost
        if s_total:
            self.vals['total'][0].set('{} ({:.1f}%)'.format(
                self.sess_lost, 100.0 * self.sess_lost / s_total))
        else:
            self.vals['total'][0].set('0')
        if self.sess_started:
            span = time.time() - self.sess_started
            self.vals['elapsed'][0].set('{:d}:{:02d}'.format(int(span // 60), int(span % 60)))
        else:
            self.vals['elapsed'][0].set('—')
        self.vals['peak'][0].set(str(self.peak))
        self.vals['mtu'][0].set('—' if self._mtu is None else str(self._mtu))

    def _draw(self):
        c = self.canvas
        c.delete('all')
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 100)
        for i in range(1, 4):
            y = h * i / 4
            c.create_line(0, y, w, y, fill='#eeeeee')
        data = list(self.samples)
        if len(data) < 2:
            c.create_text(w / 2, h / 2, text='等待数据…', fill='#999999')
            return
        lo, hi = min(data), max(data)
        if hi - lo < 50:
            mid = (hi + lo) / 2
            lo, hi = mid - 25, mid + 25
        span = hi - lo
        step = max(1, len(data) // (w * 1.2))
        pts = []
        n = len(data)
        for i in range(0, n, step):
            x = w * i / max(1, n - 1)
            y = h - (data[i] - lo) / span * h
            pts.extend((x, y))
        color = '#b4453c' if self.peak > 20000 else '#1f6f8b'
        if len(pts) >= 4:
            c.create_line(*pts, fill=color, width=1.4)
        tag = '峰值 {}：疑似饱和/未贴身，波形不是心电'.format(self.peak) if self.peak > 20000 \
            else '峰峰值 {}'.format(self.peak)
        c.create_text(8, 10, anchor='nw', text=tag, fill=color,
                      font=('Microsoft YaHei UI', 9))

    # -- 小工具 -------------------------------------------------------------
    def _set_stage(self, text, fg, bg):
        self.stage.set(text)
        self.stage_lbl.configure(fg=fg, bg=bg)

    def _append(self, text):
        stamp = datetime.now().strftime('%H:%M:%S')
        self.log.insert('end', '[{}] {}\n'.format(stamp, text))
        self.log.see('end')


def main():
    ap = argparse.ArgumentParser(description='心电贴检测工具')
    ap.add_argument('--auto', default='', help='启动后自动扫描并连接该设备名')
    ap.add_argument('--device', default='', help='预选设备名，不自动连接')
    ap.add_argument('--demo', action='store_true', help='合成数据演示，不连接真实设备')
    ap.add_argument('--repeat', type=int, default=0,
                    help='连满 N 次后停止并统计成功率（0 = 一直重连）')
    args = ap.parse_args()

    # 声明 DPI 感知，否则 150%/200% 缩放下窗口会被系统拉伸、截图也错位
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                      # noqa: BLE001
        pass

    root = tk.Tk()
    App(root, auto_name=args.auto, preselect=args.device, demo=args.demo,
        repeat=args.repeat)
    root.protocol('WM_DELETE_WINDOW', lambda: (root.destroy(), os._exit(0)))
    root.mainloop()


if __name__ == '__main__':
    sys.exit(main() or 0)
