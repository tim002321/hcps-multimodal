# -*- coding: utf-8 -*-
"""批量处理：一个大目录下的所有会话，输出镜像原目录结构的树。

模型只加载一次，各会话复用；单个会话出错不影响其余会话。
"""
from __future__ import annotations

import csv
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import report as report_mod
from .landmarks import LandmarkEngine
from .output import OutputLayout
from .params import RunParams
from .pipeline import Analyzer, RunStats
from .session import SessionEntry, SessionInfo, discover_session

SUMMARY_NAME = '_batch_summary.csv'


@dataclass
class BatchStats:
    sessions: int = 0
    done_sessions: int = 0
    failed_sessions: int = 0
    total_frames: int = 0
    done_frames: int = 0
    face_ok: int = 0
    no_face: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    elapsed_s: float = 0.0
    current: str = ''          # 当前会话的相对路径
    current_index: int = 0

    @property
    def fps(self) -> float:
        return self.done_frames / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            'sessions': self.sessions, 'done_sessions': self.done_sessions,
            'failed_sessions': self.failed_sessions,
            'total_frames': self.total_frames, 'done_frames': self.done_frames,
            'face_ok': self.face_ok, 'no_face': self.no_face,
            'skipped': self.skipped, 'failed': self.failed,
            'cancelled': self.cancelled, 'elapsed_s': round(self.elapsed_s, 2),
            'fps': round(self.fps, 2),
        }


class BatchRunner:
    def __init__(self, entries: List[SessionEntry], params: RunParams, out_root: str,
                 on_progress: Optional[Callable[[BatchStats, SessionInfo, RunStats, str],
                                                None]] = None,
                 on_log: Optional[Callable[[str], None]] = None):
        self.entries = entries
        self.params = params
        self.out_root = os.path.abspath(out_root)
        self.on_progress = on_progress
        self.on_log = on_log or (lambda _m: None)
        self.stats = BatchStats(sessions=len(entries),
                                total_frames=sum(e.est_frames for e in entries))
        self._stop = threading.Event()
        self._chain = None
        self._engine: Optional[LandmarkEngine] = None
        self._current: Optional[Analyzer] = None
        self.rows: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        self.on_log(message)

    def stop(self) -> None:
        self._stop.set()
        if self._current is not None:
            self._current.stop()

    def prepare(self) -> None:
        from .detector import DetectorChain
        started = time.time()
        self.log('加载人脸检测模型（后端={}）…'.format(self.params.backend))
        self._chain = DetectorChain(self.params).load()
        self.log('检测后端就绪：{} / {}（{:.1f}s）'.format(
            self._chain.name, self._chain.device, time.time() - started))
        engine = LandmarkEngine(self.params.resolved_models_dir())
        engine.load()
        self._engine = engine
        self.log('68 点模型就绪')
        self.log('共 {} 个会话，约 {} 帧'.format(self.stats.sessions, self.stats.total_frames))

    # ------------------------------------------------------------------
    def run(self) -> BatchStats:
        if self._chain is None:
            self.prepare()

        started = time.time()
        for index, entry in enumerate(self.entries, 1):
            if self._stop.is_set():
                self.stats.cancelled = True
                self.log('已停止：完成 {} / {} 个会话'.format(
                    self.stats.done_sessions, self.stats.sessions))
                break

            self.stats.current_index = index
            self.stats.current = entry.rel
            self.log('[{} / {}] {}'.format(index, self.stats.sessions, entry.rel))

            row = self._run_one(entry, index)
            self.rows.append(row)
            self._emit(stage='session_done')

        self.stats.elapsed_s = time.time() - started
        self._write_summary()
        self._emit(stage='done')
        return self.stats

    # ------------------------------------------------------------------
    def _run_one(self, entry: SessionEntry, index: int) -> Dict[str, Any]:
        row = {'rel_path': entry.rel, 'session': os.path.basename(entry.dir),
               'kind': entry.kind, 'status': 'failed', 'total': 0, 'done': 0,
               'face_ok': 0, 'no_face': 0, 'skipped': 0, 'failed': 0,
               'elapsed_s': 0.0, 'output_dir': '', 'note': ''}
        started = time.time()
        try:
            info = discover_session(entry.dir)
            if info.problem:
                row['note'] = info.problem
                self.stats.failed_sessions += 1
                self.log('  跳过：{}'.format(info.problem))
                return row

            info.rel_path = entry.rel

            # 单会话的帧数可能和估算不同（比如库里有些行没有文件）
            delta = info.total - entry.est_frames
            self.stats.total_frames = max(0, self.stats.total_frames + delta)

            layout = OutputLayout(self.out_root, entry.rel, self.params)
            layout.open(info={
                'session': info.session_dir, 'frames_dir': info.frames_dir,
                'db': info.db_path, 'total': info.total,
                'prefixes': dict(info.prefix_counts), 'batch_root': self.out_root,
            })

            analyzer = Analyzer(info, self.params, layout,
                                on_progress=lambda s, st: self._on_session_progress(info, s), 
                                on_log=lambda m: self.log('    ' + m),
                                chain=self._chain, engine=self._engine)
            self._current = analyzer
            stats = analyzer.run()
            self._current = None

            status = 'cancelled' if stats.cancelled else 'finished'
            summary = report_mod.build_summary(
                info, stats, self.params, layout.dir,
                {'used': self._chain.name, 'requested': self.params.backend,
                 'device': self._chain.device},
                layout.written)
            layout.finalize(status, stats.as_dict(), summary)

            row.update({
                'status': status, 'total': stats.total, 'done': stats.done,
                'face_ok': stats.face_ok, 'no_face': stats.no_face,
                'skipped': stats.skipped, 'failed': stats.failed,
                'elapsed_s': round(time.time() - started, 2),
                'output_dir': layout.dir,
            })
            self.stats.done_sessions += 1
            self.stats.face_ok += stats.face_ok
            self.stats.no_face += stats.no_face
            self.stats.skipped += stats.skipped
            self.stats.failed += stats.failed
            self.log('    完成：成功 {} ｜ 无人脸 {} ｜ {:.1f} 秒'.format(
                stats.face_ok, stats.no_face, stats.elapsed_s))
            return row
        except Exception as exc:                       # noqa: BLE001
            self._current = None
            row['note'] = str(exc)[:200]
            row['elapsed_s'] = round(time.time() - started, 2)
            self.stats.failed_sessions += 1
            self.log('    失败：{}'.format(exc))
            return row

    # ------------------------------------------------------------------
    def _on_session_progress(self, info: SessionInfo, stats: RunStats) -> None:
        self.stats.done_frames = self._finished_frames() + stats.done
        if self.on_progress is not None:
            self.on_progress(self.stats, info, stats, 'running')

    def _finished_frames(self) -> int:
        return sum(int(r.get('total') or 0) for r in self.rows)

    def _emit(self, stage: str) -> None:
        if self.on_progress is not None:
            self.on_progress(self.stats, None, None, stage)

    # ------------------------------------------------------------------
    def _write_summary(self) -> None:
        path = os.path.join(self.out_root, SUMMARY_NAME)
        try:
            os.makedirs(self.out_root, exist_ok=True)
            columns = ['rel_path', 'session', 'kind', 'status', 'total', 'done',
                       'face_ok', 'no_face', 'skipped', 'failed', 'elapsed_s',
                       'output_dir', 'note']
            with open(path, 'w', newline='', encoding='utf-8-sig') as fp:
                writer = csv.DictWriter(fp, fieldnames=columns, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(self.rows)
        except OSError as exc:
            self.log('写批量汇总失败：{}'.format(exc))
