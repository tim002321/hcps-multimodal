# -*- coding: utf-8 -*-
"""取景检查：抓几帧，画人脸/眼框/瞳孔，打印尺度，判断能不能做瞳孔测量。

用法：
    python check_view.py                # 抓 10 帧，挑最清楚的一张
    python check_view.py --frames 20
    python check_view.py --from-file <图片>   # 检查已有图片
"""
from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eyetool.capture import FfmpegCapture, ImageFolderCapture    # noqa: E402
from eyetool.eye_roi import eye_boxes                            # noqa: E402
from eyetool.params import Params                                # noqa: E402
from eyetool.pipeline import annotate                            # noqa: E402
from eyetool.pupil import detect as detect_pupil                 # noqa: E402
from eyetool.scrfd import Scrfd                                  # noqa: E402

TARGET_FACE = 450     # 人脸框宽度目标（px）
MIN_PUPIL = 20        # 瞳孔直径可用下限（px）


def verdict(face_w, eye_w, pupil_px):
    """返回给画面 HUD 用的英文结论（cv2.putText 不支持中文）。"""
    if face_w < TARGET_FACE * 0.6:
        return 'TOO FAR - move closer'
    if pupil_px < MIN_PUPIL:
        return 'PUPIL SMALL - %.0f px, need %d' % (pupil_px, MIN_PUPIL)
    return 'OK - pupil %.0f px' % pupil_px


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=10)
    ap.add_argument('--from-file', default='')
    ap.add_argument('--out', default=os.path.join(HERE, 'out', 'view_check.png'))
    args = ap.parse_args()

    p = Params().resolve_paths(HERE)
    det = Scrfd(p.detect.face_model, conf=0.4)

    best = None
    if args.from_file:
        img = cv2.imdecode(np.fromfile(args.from_file, np.uint8), cv2.IMREAD_COLOR)
        frames = [img]
    else:
        cap = FfmpegCapture(p.capture).start()
        frames = []
        for _ in range(args.frames):
            f, idx, t = cap.read()
            if f is None:
                break
            frames.append(f)
        cap.stop()

    for img in frames:
        if img is None:
            continue
        if not best:
            best = img
            continue
        if abs(float(img.mean()) - 120) < abs(float(best.mean()) - 120):
            best = img

    if best is None:
        raise SystemExit('没有取到画面')

    faces = det.detect(best)
    face = det.largest(faces)
    if face is None:
        print('没有检测到人脸：请正对镜头（不要侧脸），并让光线照到脸上')
        return

    box = face['box']
    face_w = float(box[2] - box[0])
    ipd = float(np.linalg.norm(face['kps'][0] - face['kps'][1]))
    eye_w_est = ipd * 0.45

    boxes = eye_boxes(face['kps'], best.shape,
                      pad_w=p.detect.eye_pad_w, pad_h=p.detect.eye_pad_h)
    pupils = {}
    for b in boxes:
        roi = best[b.y1:b.y2, b.x1:b.x2]
        if roi.ndim == 3:
            roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        pr = detect_pupil(roi, p.pupil, None, upscale=p.detect.upscale)
        if pr['ok']:
            pr['cx'] += b.x1
            pr['cy'] += b.y1
        pupils[b.name] = pr

    d_vals = [v['d_px'] for v in pupils.values() if v['ok']]
    pupil_px = float(np.mean(d_vals)) if d_vals else 0.0

    lines = [
        'frame %dx%d  face %.0f px  ipd %.0f px' % (
            best.shape[1], best.shape[0], face_w, ipd),
        'eye %.0f px  pupil %.0f px  (need >=%d)' % (
            eye_w_est, pupil_px, MIN_PUPIL),
        verdict(face_w, eye_w_est, pupil_px),
    ]
    print('\n'.join(lines))

    for b in boxes:
        if b.name in pupils:
            pupils[b.name]['roi'] = b.as_tuple()

    vis = annotate(best, {'face_box': box, 'boxes': boxes, 'eyes': pupils}, lines)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    cv2.imencode('.png', vis)[1].tofile(args.out)
    print('标注图：%s' % args.out)


if __name__ == '__main__':
    main()
