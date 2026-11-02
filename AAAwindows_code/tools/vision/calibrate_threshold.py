#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""张嘴阈值标定。

为什么必须做：HippoYD 是在车载 YawDD 数据上训的，作者用的判定阈值是 0.2，
那套语料的距离、光照、镜头角度和我们实验室都不一样。直接沿用要么漏检、
要么把说话全判成哈欠。所以拿本地素材扫一遍阈值，再写回 config.ini。

素材准备（每类 50 张以上更有参考价值）：

    D:/calib/
    ├── closed/    闭嘴静坐、说话（不该判为哈欠）
    ├── talking/   说话（可选，单独放能看出误报主要来自哪里）
    └── yawning/   真实打哈欠（含张嘴过程）

用法：

    python tools/vision/calibrate_threshold.py --data D:/calib
    python tools/vision/calibrate_threshold.py --data D:/calib --backend onnx

输出：每个阈值下的精确率/召回率/F1，并给出建议阈值。
注意它是「单帧」标定，只管张嘴判定的阈值；
「持续多久算一次哈欠」由 config.ini 的 yawn_min_sec 控制，需要看真实视频调。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

POSITIVE_DIRS = ('yawning', 'yawn', 'open')
NEGATIVE_DIRS = ('closed', 'close', 'talking', 'normal', 'idle')
IMAGE_PATTERNS = ('*.jpg', '*.jpeg', '*.png', '*.bmp')


def collect_images(root: str) -> Dict[str, List[str]]:
    """按一级子目录名归类。"""
    groups: Dict[str, List[str]] = {}
    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder):
            continue
        paths: List[str] = []
        for pattern in IMAGE_PATTERNS:
            paths.extend(glob.glob(os.path.join(folder, pattern)))
        if paths:
            groups[entry.lower()] = sorted(paths)
    return groups


def classify_groups(groups: Dict[str, List[str]]) -> Tuple[List[str], List[str]]:
    positives: List[str] = []
    negatives: List[str] = []
    for name, paths in groups.items():
        if name in POSITIVE_DIRS:
            positives.extend(paths)
        elif name in NEGATIVE_DIRS:
            negatives.extend(paths)
        else:
            print('  [跳过] 目录 "{}" 无法归类（正类名 {} / 负类名 {}）'.format(
                name, '/'.join(POSITIVE_DIRS), '/'.join(NEGATIVE_DIRS)))
    return positives, negatives


def score_files(paths: List[str], detector, classifier) -> List[float]:
    """逐图算张嘴概率；没检出人脸的记 -1，统计时单独计数。"""
    import cv2

    scores: List[float] = []
    missed = 0
    for path in paths:
        image = cv2.imread(path)
        if image is None:
            continue
        faces = detector.detect(image)
        if not faces:
            missed += 1
            scores.append(-1.0)
            continue
        box = faces[0]
        roi = image[box.y1:box.y2, box.x1:box.x2]
        if not roi.size:
            missed += 1
            scores.append(-1.0)
            continue
        scores.append(classifier.predict(roi))
    if missed:
        print('    （{} 张未检出人脸，按未命中计入）'.format(missed))
    return scores


def sweep(pos: List[float], neg: List[float]) -> None:
    """在 0.05~0.95 上扫阈值，打印 P/R/F1。"""
    print('\n=== 阈值扫描 ===')
    print('  {:>6}  {:>8}  {:>8}  {:>8}  {:>6}  {:>6}'.format(
        '阈值', '精确率', '召回率', 'F1', '误报', '漏检'))
    best = None
    for step in range(1, 20):
        threshold = step * 0.05
        tp = sum(1 for value in pos if value >= threshold)
        fn = len(pos) - tp
        fp = sum(1 for value in neg if value >= threshold)
        tn = len(neg) - fp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        star = ''
        if best is None or f1 > best[1]:
            best = (threshold, f1, precision, recall)
            star = ''
        print('  {:>6.2f}  {:>8.3f}  {:>8.3f}  {:>8.3f}  {:>6d}  {:>6d}{}'.format(
            threshold, precision, recall, f1, fp, fn, star))

    if best is None or best[1] <= 0:
        print('\n[失败] 所有阈值下 F1 都是 0 —— 素材可能有问题（人脸没检出 / 归类放反了）')
        return

    threshold, f1, precision, recall = best
    print('\n=== 建议 ===')
    print('  阈值 {:.2f}：精确率 {:.3f} / 召回率 {:.3f} / F1 {:.3f}'.format(
        threshold, precision, recall, f1))
    print('  把它写进 config.ini：')
    print('      [vision]')
    print('      threshold={:.2f}'.format(threshold))
    print('\n  注意：这是单帧标定结果。实际还要按视频调 yawn_min_sec（连续张嘴多久算一次），')
    print('  建议先用 1.5 秒，再用 tools/vision/bench_yawn.py 或现场实跑验证误报。')


def main() -> int:
    parser = argparse.ArgumentParser(description='张嘴判定阈值标定')
    parser.add_argument('--data', required=True, help='素材根目录（含 yawning/ 与 closed/ 等子目录）')
    parser.add_argument('--backend', default='auto', choices=('auto', 'torch', 'onnx'))
    parser.add_argument('--conf', type=float, default=0.35, help='人脸检测置信度阈值')
    args = parser.parse_args()

    if not os.path.isdir(args.data):
        print('[失败] 目录不存在：{}'.format(args.data))
        return 1

    groups = collect_images(args.data)
    if not groups:
        print('[失败] 目录里没有子文件夹或没有图片：{}'.format(args.data))
        return 1
    print('发现素材目录：')
    for name, paths in groups.items():
        print('  {:<12} {} 张'.format(name, len(paths)))

    positives, negatives = classify_groups(groups)
    print('\n正类（打哈欠）{} 张 / 负类（不该判为哈欠）{} 张'.format(len(positives), len(negatives)))
    if not positives or not negatives:
        print('[失败] 正负两类都要有素材才能标定')
        return 1

    from vision.face_detector import FaceDetector
    from vision.mouth_state import MouthClassifier

    print('\n=== 加载模型 ===')
    try:
        detector = FaceDetector(backend=args.backend, conf=args.conf)
        detector.load()
        print('  人脸检测：{}'.format(detector.description))
        classifier = MouthClassifier().load()
        print('  张嘴判定：{}'.format(os.path.basename(classifier.model_path)))
    except Exception as exc:                              # noqa: BLE001
        print('\n[无法加载模型] {}'.format(exc))
        return 1

    print('\n=== 打分 ===')
    print('  正类…')
    pos_scores = score_files(positives, detector, classifier)
    print('  负类…')
    neg_scores = score_files(negatives, detector, classifier)

    sweep(pos_scores, neg_scores)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
