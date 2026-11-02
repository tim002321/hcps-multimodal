# -*- coding: utf-8 -*-
"""实时探针：连续采集摄像头，逐帧打印张嘴概率，用来判断「灵不灵敏」。

为什么需要它：阈值 0.5 是中性初值，不是标定值。要判断是阈值太高、
还是模型在本地场景上本身就输出偏低，只能看真实数值 —— 纯靠调参猜没有意义。

用法：
    python tools/vision/live_probe.py --seconds 60

运行后对着摄像头正常说话、闭嘴、再认真打两个哈欠。结束时会打印：

  * 每帧的概率（也存成 CSV，可事后画图）
  * 分位数与「超过 0.2 / 0.3 / 0.5 的比例」
  * 概率最高的若干帧的人脸截图（存到 --out 目录），可直接肉眼看模型抓得准不准

判定看法：
  * 打哈欠时最高概率能到 0.7~0.95 → 模型没问题，只是阈值定高了，往下调即可
  * 打哈欠时最高也只有 0.1~0.2   → 本地场景和训练集差异大（距离太近/太远、
                                   逆光、只露半张脸），要换更贴合的阈值或改机位
  * 全程 0.00x                  → 人脸框没框住嘴，或镜头前不是正脸
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def main() -> int:
    parser = argparse.ArgumentParser(description='张嘴概率实时探针')
    parser.add_argument('--seconds', type=float, default=60.0, help='采集多少秒')
    parser.add_argument('--interval', type=float, default=0.15, help='采样间隔（秒）')
    parser.add_argument('--device', type=int, default=0, help='摄像头序号')
    parser.add_argument('--conf', type=float, default=0.35, help='人脸置信度阈值')
    parser.add_argument('--out', default='_probe', help='CSV 与截图输出目录')
    args = parser.parse_args()

    import cv2
    import numpy as np

    from vision.face_detector import FaceDetector
    from vision.mouth_state import MouthClassifier

    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, 'probe.csv')

    print('加载模型…', flush=True)
    detector = FaceDetector(backend='auto', conf=args.conf).load()
    classifier = MouthClassifier(threshold=0.5).load()
    print('  人脸：{}'.format(detector.description), flush=True)

    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        print('[错误] 打不开摄像头 {}'.format(args.device))
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    print('摄像头已打开，开始采集 {:.0f} 秒 —— 请正常说话、闭嘴，然后认真打两个哈欠\n'
          .format(args.seconds), flush=True)

    rows: List[Tuple[float, int, float, int, int, float]] = []   # t, 有脸, conf, w, h, prob
    best: List[Tuple[float, 'np.ndarray']] = []
    started = time.monotonic()
    last = 0.0
    try:
        while True:
            now = time.monotonic() - started
            if now >= args.seconds:
                break
            if now - last < args.interval:
                time.sleep(0.01)
                continue
            last = now

            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            boxes = detector.detect(frame)
            if not boxes:
                rows.append((now, 0, 0.0, 0, 0, 0.0))
                sys.stdout.write('\r  %5.1fs  无人脸                              ' % now)
                sys.stdout.flush()
                continue

            box = boxes[0]                                  # 面积最大的一张脸
            roi = frame[box.y1:box.y2, box.x1:box.x2]
            prob = classifier.predict(roi) if roi.size else 0.0
            rows.append((now, 1, box.score, box.width, box.height, prob))
            if len(best) < 8 or prob > best[-1][0]:
                best.append((prob, roi.copy()))
                best.sort(key=lambda item: item[0], reverse=True)
                del best[8:]

            bar = '#' * int(prob * 30)
            sys.stdout.write('\r  %5.1fs  脸 %dx%d  张嘴概率 %.4f |%-30s|' %
                             (now, box.width, box.height, prob, bar))
            sys.stdout.flush()
    except KeyboardInterrupt:
        print('\n[中断] 提前结束')
    finally:
        cap.release()

    print('\n')
    if not rows:
        print('[结果] 一帧都没采到')
        return 1

    with open(csv_path, 'w', encoding='utf-8') as fh:
        fh.write('t,has_face,face_conf,face_w,face_h,prob\n')
        for row in rows:
            fh.write('%.3f,%d,%.3f,%d,%d,%.6f\n' % row)

    probs = np.array([row[5] for row in rows], dtype=np.float32)
    faces = sum(row[1] for row in rows)
    print('=== 结果 ===')
    print('  采样             {} 帧，其中有人脸 {} 帧（{:.0f}%）'.format(
        len(rows), faces, faces * 100.0 / len(rows)))
    if faces:
        print('  张嘴概率         最大 {:.4f} / P95 {:.4f} / 中位 {:.4f} / 均值 {:.4f}'.format(
            float(probs.max()), float(np.percentile(probs, 95)),
            float(np.median(probs)), float(probs.mean())))
        for th in (0.2, 0.3, 0.4, 0.5, 0.7):
            print('    超过 {:.1f} 的帧   {:>3d} 帧（{:>5.1f}%）'.format(
                th, int((probs >= th).sum()), (probs >= th).sum() * 100.0 / len(rows)))
    print('  CSV             {}'.format(os.path.abspath(csv_path)))

    if best:
        print('  概率最高的截图   {}'.format(os.path.abspath(args.out)))
        for index, (prob, image) in enumerate(best):
            cv2.imwrite(os.path.join(args.out, 'top%d_%.4f.jpg' % (index + 1, prob)), image)

    print('\n判读：打哈欠时最大概率能到 0.7 以上 → 只是阈值定高了；')
    print('      最高只有 0.1~0.2 → 场景与训练集差异大，需要改机位或重新标定。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
