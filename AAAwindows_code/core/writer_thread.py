#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
写库线程模块：把「解析 / 解码 / 入库 / 落盘 / 打印」从 BLE 通知回调里剥离出去。

背景（实测踩坑）：
    Windows(bleak/WinRT) 下，如果把 SQLite 写入、JSONL 落盘、控制台打印直接放在
    通知回调（事件循环线程）里执行，事件循环会被周期性阻塞，导致 **BLE 通知被整条丢弃**：
      - 实测手环 PPG 从标称 25 包/s(250sps) 掉到 13~15 包/s（各类型帧按同一比例丢失）；
      - 单设备、不写库时稳定 25 包/s，设备自带的「计数戳」逐包 +1，零丢失。
    因此所有重活都必须移出通知回调。

用法：
    writer = SensorWriter(recorder, flush_interval=0.5)
    writer.start()

    def on_notify(_sender, data):            # 回调里只做「入队」
        payload = bytes(data)
        writer.submit(lambda: process(payload))

    ...
    writer.stop()                            # 退出前排空队列并提交事务
"""

import queue
import threading
import time


class SensorWriter:
    """单线程消费者：串行执行提交进来的任务，并周期性提交数据库事务。

    - `submit(fn)` 非阻塞，可在 BLE 通知回调（事件循环线程）中安全调用；
    - 所有 `fn` 都在同一个后台线程里执行，因此 SQLite 写入天然串行、无并发问题；
    - 队列积压超过上限时丢弃最旧任务，避免内存无限增长（同时计入统计）。
    """

    def __init__(self, recorder, flush_interval=0.5, max_queue=20000):
        self.recorder = recorder
        self.flush_interval = flush_interval
        self.queue = queue.Queue(maxsize=max_queue)
        self.max_queue = max_queue
        self._stop_event = threading.Event()
        self._thread = None
        self.stats = {
            "submitted": 0,     # 入队任务数
            "done": 0,          # 执行完成数
            "dropped": 0,       # 因队列满被丢弃的任务数
            "errors": 0,        # 执行出错次数
            "max_qsize": 0,     # 队列积压峰值（0 表示从未积压）
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="sensor-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout=10.0):
        """停止线程：先让线程把队列排空，再提交事务后退出。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ------------------------------------------------------------------
    # 供通知回调调用
    # ------------------------------------------------------------------
    def submit(self, fn):
        """非阻塞入队一个零参可调用对象（解析/入库/打印等重活）。"""
        try:
            self.queue.put_nowait(fn)
            self.stats["submitted"] += 1
        except queue.Full:
            # 队列已满：丢弃最旧任务，优先保证新数据入队
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.stats["dropped"] += 1
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(fn)
                self.stats["submitted"] += 1
            except queue.Full:
                self.stats["dropped"] += 1
                return
        qsize = self.queue.qsize()
        if qsize > self.stats["max_qsize"]:
            self.stats["max_qsize"] = qsize

    def flush(self):
        """立即提交数据库事务。"""
        self._flush()

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def _run(self):
        last_flush = time.time()
        while True:
            try:
                fn = self.queue.get(timeout=0.05)
            except queue.Empty:
                fn = None

            if fn is not None:
                try:
                    fn()
                    self.stats["done"] += 1
                except Exception as e:  # 单个任务失败不影响后续任务
                    self.stats["errors"] += 1
                    print("[写库线程] 任务执行出错: {}".format(e), flush=True)
                finally:
                    self.queue.task_done()

            now = time.time()
            if now - last_flush >= self.flush_interval:
                self._flush()
                last_flush = now

            # 收到停止信号且队列已排空 → 退出
            if fn is None and self._stop_event.is_set() and self.queue.empty():
                break

        self._flush()

    def _flush(self):
        try:
            self.recorder.flush()
        except Exception as e:
            self.stats["errors"] += 1
            print("[写库线程] 提交事务失败: {}".format(e), flush=True)

    def stats_line(self):
        """一行统计信息，便于日志/排查。"""
        s = self.stats
        return "入队={submitted} 完成={done} 丢弃={dropped} 出错={errors} 积压峰值={max_qsize}".format(**s)
