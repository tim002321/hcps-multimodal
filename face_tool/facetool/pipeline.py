# -*- coding: utf-8 -*-
"""主流水线：编排检测 → 68 点 → ROI 落盘 → 指标。

只在这里持有 stop Event 与计数器；不 import 任何 GUI 库。
"""
from __future__ import annotations

import os
import platform
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from . import annotate, geometry, landmarks as lm_mod, naming, quality
from .detector import DetectorChain, Face, pick_primary
from .output import OutputLayout
from .params import RunParams
from .paths import imread_unicode, last_error
from .session import FrameRef, SessionInfo

PROGRESS_EVERY = 5        # 每 N 帧回一次进度
PROGRESS_MIN_INTERVAL = 0.2


@dataclass
class RunStats:
    total: int = 0
    done: int = 0
    skipped: int = 0
    face_ok: int = 0
    no_face: int = 0
    failed: int = 0
    implausible: int = 0
    missing: int = 0
    cancelled: bool = False
    elapsed_s: float = 0.0

    @property
    def fps(self) -> float:
        return self.done / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    @property
    def eta_s(self) -> float:
        if self.done <= 0 or self.elapsed_s <= 0:
            return 0.0
        return self.remaining / max(self.done / self.elapsed_s, 1e-6)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'total': self.total, 'done': self.done, 'skipped': self.skipped,
            'face_ok': self.face_ok, 'no_face': self.no_face, 'failed': self.failed,
            'implausible': self.implausible, 'missing': self.missing,
            'cancelled': self.cancelled, 'elapsed_s': round(self.elapsed_s, 2),
            'fps': round(self.fps, 2),
        }


class Analyzer:
    def __init__(self, info: SessionInfo, params: RunParams, layout: OutputLayout,
                 on_progress: Optional[Callable[[RunStats, str], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 chain: Optional[DetectorChain] = None,
                 engine: Optional[lm_mod.LandmarkEngine] = None):
        self.info = info
        self.params = params
        self.layout = layout
        self.on_progress = on_progress
        self.on_log = on_log or (lambda _m: None)
        self.stats = RunStats(total=info.total)
        self._stop = threading.Event()
        self._chain: Optional[DetectorChain] = chain
        self._engine: Optional[lm_mod.LandmarkEngine] = engine
        self._t0_ref: Optional[float] = None
        self._last_epoch: Optional[float] = None
        self._last_emit = 0.0

    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        self.on_log(message)

    def stop(self) -> None:
        self._stop.set()

    def prepare(self) -> None:
        started = time.time()
        if self._chain is None:
            self.log('加载人脸检测模型（后端={}）…'.format(self.params.backend))
            self._chain = DetectorChain(self.params).load()
            self.log('检测后端就绪：{} / {}（{:.1f}s）'.format(
                self._chain.name, self._chain.device, time.time() - started))

        if self._engine is None:
            engine = lm_mod.LandmarkEngine(self.params.resolved_models_dir())
            engine.load()
            self._engine = engine
            self.log('68 点模型就绪：{}'.format(os.path.basename(engine.model_path)))

        self.layout.note_backend({
            'requested': self.params.backend,
            'used': self._chain.name,
            'device': self._chain.device,
        })
        self.layout.note_models(self._model_info())
        self.layout.note_env({
            'python': sys.version.split()[0],
            'platform': platform.platform(),
            'opencv': _safe_version('cv2'),
            'dlib': _safe_version('dlib'),
            'torch': _safe_version('torch'),
            'cuda': _cuda_available(),
        })

    def _model_info(self) -> List[Dict[str, Any]]:
        out = []
        for name in ('yolov8n-face.pt', 'yolov8n-face.onnx',
                     lm_mod.MODEL_NAME):
            path = os.path.join(self.params.resolved_models_dir(), name)
            item = {'name': name, 'path': path,
                    'exists': os.path.exists(path)}
            if item['exists']:
                item['size'] = os.path.getsize(path)
            out.append(item)
        return out

    # ------------------------------------------------------------------
    def run(self) -> RunStats:
        if self._chain is None:
            self.prepare()

        kinds = ['eye_l', 'eye_r', 'mouth'] + (['ann'] if self.params.annotate else [])
        resume_hash = self.layout.previous_params_hash
        can_skip = bool(resume_hash) and resume_hash == self.params.params_hash()
        if resume_hash and not can_skip:
            self.log('参数已变更（{} → {}），本次全量重算'.format(
                resume_hash, self.params.params_hash()))
        if not can_skip:
            self.log('不复用旧产物：本次结果将覆盖同名文件')

        frames = self.info.frames
        if self.params.limit:
            frames = frames[:self.params.limit]
            self.stats.total = len(frames)

        started = time.time()
        for index, ref in enumerate(frames):
            if self._stop.is_set():
                self.stats.cancelled = True
                self.log('已停止（处理到第 {} / {} 帧）'.format(index, len(frames)))
                break
            try:
                self._process_one(ref, kinds, can_skip)
            except Exception as exc:                   # noqa: BLE001 - 单帧不能拖垮整批
                self.stats.failed += 1
                detail = traceback.format_exc()
                self.layout.log_error(ref.name,
                                      '未捕获异常：{}\n{}'.format(exc, detail))
                self.layout.write_metrics({
                    'frame': ref.name, 'status': 'io_error', 'note': str(exc)[:120],
                })
            self.stats.done = index + 1
            self._emit(force=False)

        self.stats.elapsed_s = time.time() - started
        self._emit(force=True, stage='done')
        return self.stats

    # ------------------------------------------------------------------
    def _process_one(self, ref: FrameRef, kinds: List[str], can_skip: bool) -> None:
        base = self._base_row(ref)
        frame_name = self.layout.frame_name_for(ref.name)

        if can_skip and self.layout.has_all(frame_name, kinds):
            self.stats.skipped += 1
            return

        image = self._load(ref)
        if image is None:
            self.stats.missing += 1
            base.update({'status': 'file_missing', 'note': last_error()[:120]})
            self.layout.write_metrics(base)
            return

        base['width'], base['height'] = image.shape[1], image.shape[0]
        t_start = time.perf_counter()

        faces = self._chain.detect(image)
        base['n_faces'] = len(faces)
        face = pick_primary(faces, image.shape[1], self.params.face_pick,
                            self.params.center_band)
        if face is None:
            self.stats.no_face += 1
            base.update({'status': 'no_face', 'note': '没检测到人脸'})
            self.layout.write_metrics(base)
            return

        if face.width < self.params.min_face_px:
            self.stats.no_face += 1
            base.update({'status': 'face_too_small',
                         'face_w': face.width, 'face_h': face.height,
                         'note': '脸宽 {} < {}'.format(face.width, self.params.min_face_px)})
            self.layout.write_metrics(base)
            return

        pts = self._engine.predict(image, face) if self._engine else None
        ok, why = lm_mod.validate(pts, image.shape)
        base['lm_valid'] = 1 if ok else 0
        if not ok:
            self.stats.implausible += 1
            base.update({'status': 'implausible', 'note': why,
                         'face_w': face.width, 'face_h': face.height,
                         'face_conf': round(face.score, 4)})
            self.layout.write_metrics(base)
            return

        base.update(self._face_columns(face))
        base.update(quality.face_quality(image, face))
        base.update(quality.head_pose(pts, face, image.shape[1]))

        rects = {
            'eye_l': geometry.eye_roi(pts, 'L', face, self.params),
            'eye_r': geometry.eye_roi(pts, 'R', face, self.params),
            'mouth': geometry.mouth_roi(pts, self.params),
        }

        written_ok = True
        for kind, rect in rects.items():
            patch = geometry.crop(image, rect)
            if patch.size == 0:
                written_ok = False
                continue
            patch = geometry.scale_roi(patch, self.params.roi_scale)
            if not self.layout.write_image(kind, frame_name, patch):
                written_ok = False

        if self.params.annotate:
            marked = annotate.draw_landmarks(image, pts, self.params.annotate_index)
            self.layout.write_image('ann', frame_name, marked)

        ear_l = geometry.ear(pts, geometry.LEFT_EYE)
        ear_r = geometry.ear(pts, geometry.RIGHT_EYE)
        mar_in = geometry.mar_inner(pts)
        mar_out = geometry.mar_outer(pts)
        plausible = geometry.plausible_ear(ear_l) and geometry.plausible_ear(ear_r) \
            and geometry.plausible_mar(mar_in)

        base.update({
            'status': 'ok' if written_ok else 'io_error',
            'roi_source': self.params.roi_mode,
            'eye_l_x1': rects['eye_l'].x1, 'eye_l_y1': rects['eye_l'].y1,
            'eye_l_w': rects['eye_l'].width, 'eye_l_h': rects['eye_l'].height,
            'eye_r_x1': rects['eye_r'].x1, 'eye_r_y1': rects['eye_r'].y1,
            'eye_r_w': rects['eye_r'].width, 'eye_r_h': rects['eye_r'].height,
            'mouth_x1': rects['mouth'].x1, 'mouth_y1': rects['mouth'].y1,
            'mouth_w': rects['mouth'].width, 'mouth_h': rects['mouth'].height,
            'ear_l': round(ear_l, 4), 'ear_r': round(ear_r, 4),
            'ear_mean': round((ear_l + ear_r) / 2.0, 4),
            'mar_inner': round(mar_in, 4), 'mar_outer': round(mar_out, 4),
            'process_ms': round((time.perf_counter() - t_start) * 1000.0, 1),
        })
        if not written_ok:
            base['note'] = '部分图片写盘失败，见 errors.log'
        elif not plausible:
            base['note'] = '指标越界（EAR/MAR 异常），仍已落盘'

        self.stats.face_ok += 1
        self.layout.write_metrics(base)
        self.layout.write_landmarks(frame_name, ref.time, pts)

    # ------------------------------------------------------------------
    def _load(self, ref: FrameRef) -> Optional[np.ndarray]:
        if ref.blob:
            import cv2
            buf = np.frombuffer(ref.blob, dtype=np.uint8)
            return cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if ref.path:
            return imread_unicode(ref.path)
        # 老会话：图在库里（jpeg BLOB），按 id 单张取，不整批载入
        if ref.db_id and self.info.db_path:
            import cv2
            from . import db as db_mod
            blob = db_mod.fetch_blob(self.info.db_path, ref.db_id)
            if blob:
                return cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
        return None

    def _base_row(self, ref: FrameRef) -> Dict[str, Any]:
        parsed = naming.parse_stem(os.path.splitext(ref.name)[0])
        epoch = ref.time_epoch()
        if self._t0_ref is None and epoch:
            self._t0_ref = epoch
        t_rel = round(epoch - self._t0_ref, 3) if (epoch and self._t0_ref) else ''
        dt = round(epoch - self._last_epoch, 3) if (epoch and self._last_epoch) else ''
        if epoch:
            self._last_epoch = epoch
        return {
            'frame': self.layout.frame_name_for(ref.name),
            'prefix': parsed[0] if parsed else '',
            'seq': parsed[1] if parsed else '',
            'frame_index': ref.frame_index,
            'camera': ref.camera,
            'time': ref.time,
            'time_epoch': round(epoch, 3) if epoch else '',
            't_rel_s': t_rel,
            'dt_s': dt,
            'time_source': ref.time_source,
            'status': 'ok', 'note': '',
        }

    @staticmethod
    def _face_columns(face: Face) -> Dict[str, Any]:
        return {
            'face_conf': round(face.score, 4),
            'face_x1': face.x1, 'face_y1': face.y1,
            'face_x2': face.x2, 'face_y2': face.y2,
            'face_w': face.width, 'face_h': face.height,
        }

    def _emit(self, force: bool = False, stage: str = 'running') -> None:
        if self.on_progress is None:
            return
        now = time.time()
        if not force and (now - self._last_emit) < PROGRESS_MIN_INTERVAL \
                and (self.stats.done % PROGRESS_EVERY) != 0:
            return
        self._last_emit = now
        self.stats.elapsed_s = max(self.stats.elapsed_s, 0.0)
        self.on_progress(self.stats, stage)


# ---------------------------------------------------------------------- 小工具
def _safe_version(module: str) -> str:
    try:
        mod = __import__(module)
        return str(getattr(mod, '__version__', '?'))
    except Exception:                                  # noqa: BLE001
        return ''


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:                                  # noqa: BLE001
        return False
