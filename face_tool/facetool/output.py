# -*- coding: utf-8 -*-
"""落盘：子目录布局、CSV、run.json、错误日志。

文件名与源帧**逐字节一致**（含扩展名），靠子目录区分类型，所以同一帧的多个产物
不可能互相覆盖，也能直接和数据库里的 file_name 对上。
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Mapping, Sequence

from . import __version__, naming
from .params import RunParams
from .paths import ensure_dir, imwrite_unicode, last_error

SUBDIRS = {
    'eye_l': 'eyes_left',
    'eye_r': 'eyes_right',
    'mouth': 'mouth',
    # 标注图=原图副本，含人脸，只在显式开启时生成，目录名也点明"仅自查"
    'ann': 'landmarks_check',
    'noface': 'no_face',
}

METRIC_COLUMNS: List[str] = [
    'frame', 'prefix', 'seq', 'frame_index', 'camera',
    'time', 'time_epoch', 't_rel_s', 'dt_s', 'time_source',
    'width', 'height', 'status', 'note',
    'n_faces', 'face_conf', 'face_x1', 'face_y1', 'face_x2', 'face_y2',
    'face_w', 'face_h', 'face_ratio', 'roi_source',
    # ---- 质量（只能在这里算，下游无法补）----
    'blur_var', 'brightness', 'contrast', 'over_ratio', 'under_ratio', 'is_blurry',
    'roll_deg', 'yaw_ratio', 'pitch_ratio',
    # ---- ROI 在原图中的位置，便于下游反查 ----
    'eye_l_x1', 'eye_l_y1', 'eye_l_w', 'eye_l_h',
    'eye_r_x1', 'eye_r_y1', 'eye_r_w', 'eye_r_h',
    'mouth_x1', 'mouth_y1', 'mouth_w', 'mouth_h',
    'ear_l', 'ear_r', 'ear_mean', 'mar_inner', 'mar_outer',
    'lm_valid', 'process_ms',
]

LANDMARK_COLUMNS: List[str] = ['frame', 'time'] + \
    ['x{}'.format(i) for i in range(68)] + ['y{}'.format(i) for i in range(68)]


class OutputLayout:
    def __init__(self, root: str, session_name: str, params: RunParams,
                 flat: bool = False):
        self.root = os.path.abspath(root or '.')
        self.session_name = '' if flat else (session_name or 'session')
        self.params = params
        self.dir = os.path.join(self.root, self.session_name) if self.session_name \
            else self.root
        self._metrics_file = None
        self._metrics_writer = None
        self._lm_file = None
        self._lm_writer = None
        self._errors = None
        self._run_meta: Dict[str, Any] = {}
        self._previous_hash = ''
        self.written = {k: 0 for k in SUBDIRS}

    # ------------------------------------------------------------------ 路径
    def subdir(self, kind: str) -> str:
        return os.path.join(self.dir, SUBDIRS[kind])

    def target_ext(self, kind: str) -> str:
        """眼部默认存 PNG（无损，瞳孔边缘不糊），其余沿用 jpg。"""
        if kind in ('eye_l', 'eye_r') and self.params.eye_format == 'png':
            return '.png'
        return ''

    def path(self, kind: str, frame_name: str) -> str:
        name = frame_name
        ext = self.target_ext(kind)
        if ext:
            name = os.path.splitext(frame_name)[0] + ext
        return os.path.join(self.subdir(kind), name)

    def has_all(self, frame_name: str, kinds: Sequence[str]) -> bool:
        return all(os.path.exists(self.path(k, frame_name)) for k in kinds)

    # ------------------------------------------------------------------ 生命周期
    def previous_hash(self) -> str:
        meta = os.path.join(self.dir, 'run.json')
        if not os.path.exists(meta):
            return ''
        try:
            with open(meta, 'r', encoding='utf-8') as fp:
                return str(json.load(fp).get('params_hash') or '')
        except (OSError, ValueError):
            return ''

    @property
    def previous_params_hash(self) -> str:
        """open() 那一刻读到的旧指纹。注意 run.json 随后会被本轮覆盖，
        所以续跑判定必须用这个缓存值，不能重新读文件。"""
        return self._previous_hash

    def open(self, info: Mapping[str, Any] | None = None, resume_ok: bool = True) -> None:
        ensure_dir(self.dir)
        self._previous_hash = self.previous_hash() if resume_ok else ''
        for kind in ('eye_l', 'eye_r', 'mouth'):
            ensure_dir(self.subdir(kind))
        if self.params.annotate:
            ensure_dir(self.subdir('ann'))

        self._metrics_file = open(os.path.join(self.dir, 'metrics.csv'), 'w',
                                  newline='', encoding='utf-8-sig')
        self._metrics_writer = csv.DictWriter(self._metrics_file, fieldnames=METRIC_COLUMNS,
                                              extrasaction='ignore')
        self._metrics_writer.writeheader()

        self._lm_file = open(os.path.join(self.dir, 'landmarks.csv'), 'w',
                             newline='', encoding='utf-8-sig')
        self._lm_writer = csv.DictWriter(self._lm_file, fieldnames=LANDMARK_COLUMNS,
                                         extrasaction='ignore')
        self._lm_writer.writeheader()

        self._errors = open(os.path.join(self.dir, 'errors.log'), 'w', encoding='utf-8')

        self._run_meta = {
            'tool': 'face_tool',
            'version': __version__,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'running',
            'input': dict(info or {}),
            'params': self.params.to_dict(),
            'params_hash': self.params.params_hash(),
            'output_dir': self.dir,
        }
        self._write_meta()

    def _write_meta(self) -> None:
        try:
            with open(os.path.join(self.dir, 'run.json'), 'w', encoding='utf-8') as fp:
                json.dump(self._run_meta, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def note_models(self, models: List[Dict[str, Any]]) -> None:
        self._run_meta['models'] = models
        self._write_meta()

    def note_env(self, env: Mapping[str, Any]) -> None:
        self._run_meta['env'] = dict(env)
        self._write_meta()

    def note_backend(self, backend: Mapping[str, Any]) -> None:
        self._run_meta['backend'] = dict(backend)
        self._write_meta()

    # ------------------------------------------------------------------ 写
    def write_image(self, kind: str, frame_name: str, image) -> bool:
        target = self.path(kind, frame_name)
        ok = imwrite_unicode(target, image, self.params.jpeg_quality)
        if ok:
            self.written[kind] = self.written.get(kind, 0) + 1
        else:
            self.log_error(frame_name, '{} 写盘失败：{}'.format(kind, last_error()))
        return ok

    def copy_original(self, frame_name: str, image) -> bool:
        return self.write_image('noface', frame_name, image)

    def write_metrics(self, row: Mapping[str, Any]) -> None:
        if self._metrics_writer is not None:
            self._metrics_writer.writerow(dict(row))

    def write_landmarks(self, frame_name: str, time_text: str, pts) -> None:
        if self._lm_writer is None or pts is None or len(pts) != 68:
            return
        row = {'frame': frame_name, 'time': time_text}
        for i in range(68):
            row['x{}'.format(i)] = round(float(pts[i][0]), 2)
            row['y{}'.format(i)] = round(float(pts[i][1]), 2)
        self._lm_writer.writerow(row)

    def log_error(self, frame_name: str, message: str) -> None:
        if self._errors is not None:
            self._errors.write('{}\t{}\t{}\n'.format(
                datetime.now().strftime('%H:%M:%S'), frame_name, message))
            self._errors.flush()

    # ------------------------------------------------------------------ 收尾
    def finalize(self, status: str, result: Mapping[str, Any],
                 summary_text: str = '') -> None:
        for handle in (self._metrics_file, self._lm_file, self._errors):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._metrics_file = self._metrics_writer = None
        self._lm_file = self._lm_writer = None
        self._errors = None

        if summary_text:
            try:
                with open(os.path.join(self.dir, 'summary.txt'), 'w', encoding='utf-8') as fp:
                    fp.write(summary_text)
            except OSError:
                pass

        self._run_meta['status'] = status
        self._run_meta['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._run_meta['result'] = dict(result)
        self._run_meta['outputs'] = dict(self.written)
        self._write_meta()

    # ------------------------------------------------------------------ 杂
    def frame_name_for(self, source_name: str) -> str:
        """输出名 = 源名。非主程序格式的外来图也原样保留（只做非法字符兜底）。"""
        return naming.safe_filename(source_name)
