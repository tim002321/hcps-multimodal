#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打哈欠检测的离线基准与链路自检。

三种用法：

    # 1) 只验证线程/状态链路（不加载模型，任意机器都能跑）
    python tools/vision/bench_yawn.py --source stub

    # 2) 用历史会话库里的摄像头帧跑真实推理，看延迟与命中率
    python tools/vision/bench_yawn.py --source db:data/camera_20261017_150214.db --frames 120

    # 3) 用一堆图片跑（比如自己录的素材导出的帧）
    python tools/vision/bench_yawn.py --source dir:D:/yawn_frames --frames 200

它同时是一个冒烟测试：跑完会打印人脸检出率、单帧延迟分布、张嘴命中率，
以及检测到的哈欠次数。真实阈值要拿本工具在本地素材上扫一遍再定。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ---------------------------------------------------------------------------
# 假的摄像头：把一串图当作 30fps 的视频喂给监控线程
# ---------------------------------------------------------------------------
class FakeCamera:
    """模仿 CameraService 的最小接口：latest_frame() -> (jpeg, index, stamp)。"""

    def __init__(self, jpegs: List[bytes], fps: float = 30.0):
        import threading
        self._jpegs = jpegs
        self._fps = max(1.0, fps)
        self._lock = threading.Lock()
        self._latest: Optional[Tuple[bytes, int, float]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        import cv2
        import numpy as np
        period = 1.0 / self._fps
        index = 0
        while not self._stop.is_set():
            if not self._jpegs:
                time.sleep(period)
                continue
            jpeg = self._jpegs[index % len(self._jpegs)]
            index += 1
            with self._lock:
                self._latest = (jpeg, index, time.time())
            time.sleep(period)

    def latest_frame(self):
        with self._lock:
            return self._latest

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(1.0)


def make_stub_frames(size: int = 24) -> List[bytes]:
    """造几张纯色 JPEG 当占位（配合 --source stub 用）。"""
    import cv2
    import numpy as np
    frames = []
    for i in range(3):
        image = np.full((240, 320, 3), 40 + i * 30, dtype=np.uint8)
        ok, buffer = cv2.imencode('.jpg', image)
        if ok:
            frames.append(bytes(buffer))
    return frames


# ---------------------------------------------------------------------------
# 从会话库 / 目录取帧
# ---------------------------------------------------------------------------
def load_frames_from_db(db_path: str, limit: int) -> List[bytes]:
    import sqlite3

    if not os.path.exists(db_path):
        raise SystemExit('库文件不存在：{}'.format(db_path))
    conn = sqlite3.connect('file:{}?mode=ro'.format(db_path), uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        if 'camera_frames' not in tables:
            raise SystemExit('这个库里没有 camera_frames 表：{}'.format(db_path))
        rows = conn.execute(
            'SELECT jpeg FROM camera_frames ORDER BY frame_index LIMIT ?', (limit,)).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def load_frames_from_dir(folder: str, limit: int) -> List[bytes]:
    patterns = ('*.jpg', '*.jpeg', '*.png')
    paths: List[str] = []
    for pattern in patterns:
        paths.extend(glob.glob(os.path.join(folder, pattern)))
    paths.sort()
    if not paths:
        raise SystemExit('目录里没有图片：{}'.format(folder))
    frames = []
    for path in paths[:limit]:
        with open(path, 'rb') as handle:
            frames.append(handle.read())
    return frames


# ---------------------------------------------------------------------------
# 基准
# ---------------------------------------------------------------------------
def percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return ordered[index]


def bench_real(frames: List[bytes], backend: str, threshold: float, conf: float) -> int:
    import cv2
    import numpy as np

    from vision.face_detector import FaceDetector
    from vision.mouth_state import MouthClassifier
    from vision.yawn_logic import YawnLogic

    print('\n=== 加载模型 ===')
    try:
        detector = FaceDetector(backend=backend, conf=conf)
        detector.load()
        print('  人脸检测：{}'.format(detector.description))
        classifier = MouthClassifier(threshold=threshold).load()
        print('  张嘴判定：{}（阈值 {:.2f}）'.format(
            os.path.basename(classifier.model_path), threshold))
    except Exception as exc:                              # noqa: BLE001
        print('\n[无法加载模型] {}'.format(exc))
        print('\n可先用「不依赖模型」的链路自检：')
        print('    python tools/vision/bench_yawn.py --source stub')
        return 1

    logic = YawnLogic(threshold=threshold)
    face_ms: List[float] = []
    mouth_ms: List[float] = []
    total_ms: List[float] = []
    detected = 0
    mouth_open = 0

    # 预热：首帧要跑 CUDA 上下文初始化 / cuDNN 选算法 / onnx 会话首次分配，
    # 单帧能到 2~3 秒。不预热就统计，均值会被这一帧拉高好几倍，看起来像模型很慢。
    if frames:
        warm = cv2.imdecode(np.frombuffer(frames[0], dtype=np.uint8), cv2.IMREAD_COLOR)
        if warm is not None:
            for _ in range(3):
                warm_faces = detector.detect(warm)
                if warm_faces:
                    classifier.predict(warm[warm_faces[0].y1:warm_faces[0].y2,
                                             warm_faces[0].x1:warm_faces[0].x2])
            print('  预热完成（3 次，不计入统计）')

    print('\n=== 逐帧推理（{} 帧）==='.format(len(frames)))
    started = time.perf_counter()
    for index, jpeg in enumerate(frames):
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue

        t0 = time.perf_counter()
        faces = detector.detect(frame)
        t1 = time.perf_counter()
        prob = 0.0
        if faces:
            detected += 1
            main = faces[0]
            roi = frame[main.y1:main.y2, main.x1:main.x2]
            if roi.size:
                prob = classifier.predict(roi)
                if prob >= threshold:
                    mouth_open += 1
        t2 = time.perf_counter()

        face_ms.append((t1 - t0) * 1000.0)
        mouth_ms.append((t2 - t1) * 1000.0)
        total_ms.append((t2 - t0) * 1000.0)
        logic.update(index / 7.0, prob, has_face=bool(faces))   # 按 7fps 的时间轴推状态

    elapsed = time.perf_counter() - started
    total = max(1, len(frames))

    print('\n=== 结果 ===')
    print('  帧数              {}'.format(len(frames)))
    print('  人脸检出          {} 帧（{:.0f}%）'.format(detected, detected * 100.0 / total))
    print('  判为张嘴          {} 帧（{:.0f}%）'.format(mouth_open, mouth_open * 100.0 / total))
    print('  平均端到端延迟    {:.1f} ms（P50 {:.1f} / P95 {:.1f}）'.format(
        sum(total_ms) / total, percentile(total_ms, 0.5), percentile(total_ms, 0.95)))
    print('    其中人脸检测    {:.1f} ms 均值'.format(sum(face_ms) / total))
    print('    其中张嘴判定    {:.1f} ms 均值'.format(sum(mouth_ms) / total))
    print('  等效吞吐          {:.1f} fps（单线程串行）'.format(len(frames) / max(elapsed, 1e-6)))
    print('  状态机判定        哈欠 {} 次 / 疲劳 {}'.format(
        logic._status(time.time(), 'x').yawn_count,
        logic._status(time.time(), 'x').fatigue_text))

    target_ok = (sum(total_ms) / total) < 60.0
    print('\n  [{}] 单帧延迟 {} 60ms 目标'.format('OK' if target_ok else '超标',
                                            '<' if target_ok else '>='))
    return 0 if target_ok else 1


# ---------------------------------------------------------------------------
# stub：不加载模型，只验证线程 + 状态链路
# ---------------------------------------------------------------------------
class StubDetector:
    description = 'stub（未加载模型）'

    def __init__(self):
        self.last_latency_ms = 0.0

    def detect(self, frame):
        from vision.face_detector import FaceBox
        height, width = frame.shape[:2]
        return [FaceBox(width // 4, height // 4, width * 3 // 4, height * 3 // 4, 0.99)]


class StubClassifier:
    def __init__(self, pattern_seconds: float = 2.0):
        self.threshold = 0.5
        self.last_latency_ms = 0.0
        self._started = time.monotonic()
        self._period = pattern_seconds * 2      # 开一半、闭一半

    def predict(self, face_bgr) -> float:
        elapsed = time.monotonic() - self._started
        return 0.9 if (elapsed % self._period) < self._period / 2 else 0.05


def bench_stub(seconds: float) -> int:
    from vision.yawn_monitor import YawnMonitor, YawnState

    print('\n=== 链路自检（stub，不加载模型）===')
    frames = make_stub_frames()
    camera = FakeCamera(frames, fps=30.0).start()
    state = YawnState()
    monitor = YawnMonitor(camera, state, interval=0.15,
                          detector=StubDetector(), classifier=StubClassifier(2.0),
                          logic=None)
    # stub 分类器每 2 秒张嘴 / 2 秒闭口，4 秒一个周期 → 每次张嘴 2s ≥ 1.5s 应计 1 次
    monitor.start()

    print('  跑 {:.0f} 秒，观察状态变化…'.format(seconds))
    seen_states = set()
    snapshot = state.snapshot()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.2)
            snapshot = state.snapshot()
            seen_states.add(snapshot.state)
            sys.stdout.write('\r  {}/{} 帧 哈欠 {} 次  {:.1f}fps  {:.1f}ms   '.format(
                snapshot.state_text, snapshot.fatigue_text, snapshot.yawn_count,
                snapshot.fps, snapshot.latency_ms))
            sys.stdout.flush()
    finally:
        monitor.stop()
        camera.stop()

    print('\n\n=== 结果 ===')
    print('  线程已停止        {}'.format(not monitor.is_alive()))
    print('  走过的状态        {}'.format('、'.join(sorted(seen_states))))
    print('  累计哈欠          {} 次'.format(snapshot.yawn_count))
    print('  调用帧率          {:.1f} fps'.format(snapshot.fps))

    ok_thread = not monitor.is_alive()
    ok_states = {'open', 'yawning'} <= seen_states
    ok_count = snapshot.yawn_count >= 1
    for name, ok in (('线程能正常停止', ok_thread),
                     ('状态机走过「张嘴中/打哈欠」', ok_states),
                     ('按 2 秒张嘴的节奏至少计到 1 次', ok_count)):
        print('  [{}] {}'.format('OK' if ok else 'FAIL', name))
    return 0 if all((ok_thread, ok_states, ok_count)) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='打哈欠检测离线基准 / 链路自检')
    parser.add_argument('--source', default='stub',
                        help='stub | db:<会话库.db> | dir:<图片目录>')
    parser.add_argument('--frames', type=int, default=120, help='最多取多少帧')
    parser.add_argument('--seconds', type=float, default=10.0, help='stub 模式跑多久')
    parser.add_argument('--backend', default='auto', choices=('auto', 'torch', 'onnx'))
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument('--conf', type=float, default=0.35, help='人脸检测置信度阈值')
    args = parser.parse_args()

    if args.source == 'stub':
        return bench_stub(args.seconds)

    if args.source.startswith('db:'):
        frames = load_frames_from_db(args.source[3:], args.frames)
    elif args.source.startswith('dir:'):
        frames = load_frames_from_dir(args.source[4:], args.frames)
    else:
        frames = load_frames_from_dir(args.source, args.frames)

    print('取到 {} 帧'.format(len(frames)))
    if not frames:
        print('[失败] 没有取到任何帧')
        return 1
    return bench_real(frames, args.backend, args.threshold, args.conf)


if __name__ == '__main__':
    raise SystemExit(main())
