# -*- coding: utf-8 -*-
"""工作线程：跑 Analyzer（单会话）或 BatchRunner（批量）。

这个文件**不允许 import tkinter**——所有控件更新都交给主线程的 _tick。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .. import report as report_mod
from ..batch import BatchRunner
from ..output import OutputLayout
from ..params import RunParams
from ..pipeline import Analyzer, RunStats
from ..session import SessionEntry, SessionInfo


@dataclass
class ProgressState:
    stage: str = 'idle'        # idle|loading|running|done|cancelled|error
    batch: bool = False
    sessions: int = 0
    session_done: int = 0
    current_session: str = ''
    total: int = 0
    done: int = 0
    face_ok: int = 0
    no_face: int = 0
    implausible: int = 0
    failed: int = 0
    skipped: int = 0
    fps: float = 0.0
    eta_s: float = 0.0
    backend: str = ''
    message: str = ''

    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, 100.0 * self.done / self.total)


class AnalyzerWorker(threading.Thread):
    def __init__(self, info: Optional[SessionInfo], params: RunParams,
                 layout: Optional[OutputLayout], state: ProgressState,
                 lock: threading.Lock, log_q: 'queue.Queue[str]',
                 done_q: 'queue.Queue[Dict[str, Any]]',
                 entries: Optional[List[SessionEntry]] = None,
                 out_root: str = ''):
        super().__init__(name='face-tool-worker', daemon=True)
        self.info = info
        self.params = params
        self.layout = layout
        self.state = state
        self.lock = lock
        self.log_q = log_q
        self.done_q = done_q
        self.entries = entries or []
        self.out_root = out_root
        self._analyzer: Optional[Analyzer] = None
        self._runner: Optional[BatchRunner] = None

    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        for line in str(message).splitlines():
            self.log_q.put(line)

    def _set(self, **kwargs: Any) -> None:
        with self.lock:
            for key, value in kwargs.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)

    def _on_progress(self, stats: RunStats, stage: str) -> None:
        self._set(stage=stage if stage != 'running' else self.state.stage,
                  total=stats.total, done=stats.done, face_ok=stats.face_ok,
                  no_face=stats.no_face, implausible=stats.implausible,
                  failed=stats.failed, skipped=stats.skipped,
                  fps=stats.fps, eta_s=stats.eta_s)

    def _on_batch_progress(self, stats, info, session_stats, stage: str) -> None:
        self._set(batch=True,
                  stage=stage if stage in ('done', 'cancelled') else self.state.stage,
                  sessions=stats.sessions,
                  session_done=stats.done_sessions,
                  current_session=stats.current,
                  total=stats.total_frames, done=stats.done_frames,
                  face_ok=stats.face_ok, no_face=stats.no_face,
                  failed=stats.failed, skipped=stats.skipped,
                  fps=stats.fps, eta_s=0.0)

    # ------------------------------------------------------------------
    def cancel(self) -> None:
        for target in (self._analyzer, self._runner):
            if target is not None:
                target.stop()
        self._log('已请求停止…')

    def run(self) -> None:
        started = time.time()
        if self.entries:
            self._run_batch(started)
        else:
            self._run_single(started)

    # ------------------------------------------------------------------
    def _run_single(self, started: float) -> None:
        status = 'finished'
        try:
            self._set(stage='loading', total=self.info.total, done=0, batch=False)
            self.log_q.put('__stage__准备模型')
            self.layout.open(info={
                'session': self.info.session_dir,
                'frames_dir': self.info.frames_dir,
                'db': self.info.db_path,
                'total': self.info.total,
                'prefixes': dict(self.info.prefix_counts),
            })
            self._analyzer = Analyzer(self.info, self.params, self.layout,
                                      on_progress=self._on_progress, on_log=self._log)
            self._analyzer.prepare()
            self._set(stage='running', backend=self._analyzer._chain.name)
            self.log_q.put('__stage__处理中')
            stats = self._analyzer.run()
            if stats.cancelled:
                status = 'cancelled'
            summary = report_mod.build_summary(
                self.info, stats, self.params, self.layout.dir,
                {'used': self._analyzer._chain.name, 'requested': self.params.backend,
                 'device': self._analyzer._chain.device},
                self.layout.written)
            self.layout.finalize(status, stats.as_dict(), summary)
            self._set(stage='cancelled' if stats.cancelled else 'done',
                      total=stats.total, done=stats.done)
            self.done_q.put({'ok': True, 'stats': stats.as_dict(), 'status': status,
                             'dir': self.layout.dir, 'elapsed': time.time() - started})
        except Exception as exc:                        # noqa: BLE001
            self._log('[失败] {}'.format(exc))
            try:
                self.layout.finalize('failed', {'error': str(exc)})
            except Exception:                           # noqa: BLE001
                pass
            self._set(stage='error', message=str(exc))
            self.done_q.put({'ok': False, 'error': str(exc),
                             'elapsed': time.time() - started})

    # ------------------------------------------------------------------
    def _run_batch(self, started: float) -> None:
        try:
            self._set(stage='loading', batch=True, sessions=len(self.entries),
                      total=sum(e.est_frames for e in self.entries), done=0)
            self.log_q.put('__stage__准备模型')
            self._runner = BatchRunner(self.entries, self.params, self.out_root,
                                       on_progress=self._on_batch_progress,
                                       on_log=self._log)
            self._runner.prepare()
            self._set(stage='running')
            self.log_q.put('__stage__批量处理中')
            stats = self._runner.run()
            self._set(stage='cancelled' if stats.cancelled else 'done',
                      session_done=stats.done_sessions, done=stats.done_frames)
            self.done_q.put({'ok': True, 'batch': True, 'stats': stats.as_dict(),
                             'status': 'cancelled' if stats.cancelled else 'finished',
                             'dir': self.out_root, 'elapsed': time.time() - started})
        except Exception as exc:                        # noqa: BLE001
            self._log('[失败] {}'.format(exc))
            self._set(stage='error', message=str(exc))
            self.done_q.put({'ok': False, 'error': str(exc),
                             'elapsed': time.time() - started})
