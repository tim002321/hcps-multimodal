# -*- coding: utf-8 -*-
"""表情识别测试（不需要硬件，需要 tensorflow）：
真实人脸图 -> 检测框 + 表情；纯色图 -> no_face；会话库逐帧 -> 表情表。

用法：
    python tools/test_face_pipeline.py
    python tools/test_face_pipeline.py --task face_analyze --fresh

用的是老工程留下的两张真实测试图（models/test_photo2.png 是笑脸、test_photo.jpg 是证件照裁脸），
所以除了跑通链路，还顺带校验模型确实在工作：笑脸那张必须判成 happy。
（老工程的视频帧目录里没有真人素材，所以会话级那一段用测试图当帧。）
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.A0_data_collect.base import DataPacket                          # noqa: E402
from core.A1_data_store import SqliteStore, usable_session_dirs           # noqa: E402
from core.A2_analyze import EMOTIONS, FaceExpressionAnalyzer              # noqa: E402

TIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'


def count_frames(db_path: str) -> int:
    """库里 data_video_frame 已有多少行（库或表不存在就是 0）。"""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute('SELECT COUNT(*) FROM data_video_frame').fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit('缺少 PyYAML：pip install pyyaml')
    with open(path, encoding='utf-8') as fp:
        return yaml.safe_load(fp) or {}


def build_face_session(source_image: Path, frames: int = 3, task: str = 'face_analyze',
                       fresh: bool = False) -> dict:
    """建一个只含几帧视频的会话库，帧内容是同一张真实人脸图。"""
    import cv2

    dirs = usable_session_dirs(user='smoke', task=task, root=str(ROOT / '实验结果'), fresh=fresh)
    frames_dir = Path(dirs['result_dir']) / 'video1'
    frames_dir.mkdir(parents=True, exist_ok=True)
    rows_before = count_frames(dirs['db_path'])          # 复用会话目录时库里会累加

    # 会话目录带中文，cv2.imread/imwrite 在 Windows 上都不认，统一走 imdecode/imencode
    image = cv2.imdecode(np.fromfile(str(source_image), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f'读不出测试图：{source_image}')

    start = datetime(2026, 9, 15, 11, 0, 0)
    store = SqliteStore(dirs['db_path']).start()
    for i in range(1, frames + 1):
        target = frames_dir / f'1_pic_{i:06d}.jpg'
        # 重新编码成真 JPEG（别把 PNG 字节塞进 .jpg），并且用 tofile 绕开中文路径
        success, buffer = cv2.imencode('.jpg', image)
        if not success:
            raise SystemExit('JPEG 编码失败')
        buffer.tofile(str(target))
        created = (start + timedelta(seconds=i)).strftime(TIME_FORMAT)
        store.submit(DataPacket(device='video_frame',
                                payload={'camera': 'video1', 'index': i, 'path': str(target),
                                         'send_time': created},
                                t_recv=created))
    store.stop()
    return {'db_path': dirs['db_path'], 'frames_dir': str(frames_dir), 'frames': frames,
            'rows_before': rows_before,
            'start': start.strftime(TIME_FORMAT),
            'end': (start + timedelta(seconds=frames + 1)).strftime(TIME_FORMAT)}


def main() -> int:
    parser = argparse.ArgumentParser(description='A2 表情识别测试')
    parser.add_argument('--config', default=str(ROOT / 'config' / 'default.yaml'))
    parser.add_argument('--task', default='face_analyze')
    parser.add_argument('--frames', type=int, default=3, help='会话级测试造几帧')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    config = (load_config(Path(args.config)).get('face') or {})
    analyzer = FaceExpressionAnalyzer(config)
    failures = []

    def check(name: str, ok: bool, detail: str = '') -> None:
        print(f'  {"[OK]" if ok else "[!!]"} {name}{"" if ok else "  " + detail}')
        if not ok:
            failures.append(f'{name} {detail}'.strip())

    smile = Path(analyzer.models_dir) / 'test_photo2.png'
    portrait = Path(analyzer.models_dir) / 'test_photo.jpg'
    print(f'\n模型权重：{analyzer.weights_path}')
    print(f'测试图：{smile.name}（笑脸）、{portrait.name}（证件照），检测器 {analyzer.detector}/{analyzer.detector_model}')

    started = time.time()
    analyzer.load()
    print(f'CNN3 加载完成，用时 {time.time() - started:.1f} 秒')

    print('\n=== 单图识别 ===')
    ok_images = smile.exists() and portrait.exists()
    if ok_images:
        for path in (smile, portrait):
            started = time.time()
            result = analyzer.predict_path(str(path))
            print(f'  {path.name:18s} -> {result["emotions"]}  人脸 {len(result["boxes"])} 张'
                  f'  用时 {time.time() - started:.2f}s')
            if result['boxes']:
                print(f'    框 {result["boxes"][0]}，概率和 {[round(float(v), 3) for v in result["probabilities"][0]]}')
    else:
        print('  （测试图不在 models/ 下，跳过单图识别）')

    print('\n=== 会话级识别 ===')
    session = build_face_session(smile, frames=args.frames, task=args.task, fresh=args.fresh)
    print(f'  会话库：{session["db_path"]}')
    print(f'  本次写入 {session["frames"]} 帧（库里原有 {session["rows_before"]} 帧记录）')
    started = time.time()
    table = analyzer.analyze(session['db_path'], session['start'], session['end'])
    print(f'  {len(table)} 帧，用时 {time.time() - started:.1f} 秒')
    if len(table):
        print('  ' + table[['frame_index', 'emotion', 'faces']].to_string(index=False).replace('\n', '\n  '))

    print('\n=== 断言 ===')
    check('模型加载成功', analyzer._model is not None)
    if ok_images:
        smile_result = analyzer.predict_path(str(smile))
        check('笑脸图检出了人脸', len(smile_result['boxes']) >= 1, f'检出 {len(smile_result["boxes"])} 张')
        check('笑脸图判成 happy（模型真的在工作）',
              smile_result['emotions'][0] == 'happy', f'实际 {smile_result["emotions"][0]}')
        check('表情标签在 8 类里', smile_result['emotions'][0] in EMOTIONS)

    blank = np.full((200, 200, 3), 128, dtype=np.uint8)
    blank_result = analyzer.predict_image(blank)
    check('纯色图判成 no_face', blank_result['emotions'] == ['no_face'], f'实际 {blank_result["emotions"]}')
    check('no_face 时概率全 0', float(np.sum(blank_result['probabilities'][0])) == 0.0)

    expected = session['rows_before'] + session['frames']
    check('会话级逐帧出表', len(table) == expected, f'{len(table)} vs {expected}')
    check('会话表列齐全',
          list(table.columns) == ['time', 'camera', 'frame_index', 'path', 'emotion', 'faces'],
          f'实际 {list(table.columns)}')
    check('每帧都判出 8 类里的表情',
          len(table) > 0 and bool(table['emotion'].isin(EMOTIONS).all()),
          f'实际 {sorted(set(table["emotion"])) if len(table) else []}')

    ok = not failures
    print('\n[通过] 表情识别流水线正常' if ok else f'\n[失败] {len(failures)} 项没过：{failures}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
