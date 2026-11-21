#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面自检：用历史数据把三个页面填满，离屏渲染成 PNG（不需要硬件）。

用法：
    python tools/ui_screenshot.py                     # 默认取 data/ 里数据最全的库
    python tools/ui_screenshot.py --db data/xxx.db --seconds 30 --out ui_shots

顺便做几条断言（心电/手环缓冲有没有数据、心率能不能算出来），所以它既是截图工具也是冒烟测试。
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np                                                    # noqa: E402

from core.media_paths import media_root                               # noqa: E402
from ui.buffers import (ECG_RATE, LiveBuffers, ecg_signal_quality,     # noqa: E402
                        estimate_heart_rate)
from ui.replay import list_sessions, load_packets                     # noqa: E402
from ui.window import STAGE_SPECS, MonitorWindow, create_app          # noqa: E402


class FakeCamera:
    """假 USB 摄像头：按 60fps 产出带时间戳的合成画面（让 30fps 截帧逻辑有得丢）。"""

    WIDTH, HEIGHT = 640, 480

    def __init__(self, index=0, backend=0):
        import cv2
        import numpy as np

        self._cv2 = cv2
        self._np = np
        self._opened = True
        self._frames = 0
        self._last = 0.0

    def isOpened(self):                               # noqa: N802
        return self._opened

    def set(self, prop, value):                       # noqa: N802
        return True

    def read(self):                                   # noqa: N802
        time.sleep(1 / 60.0)                          # 模拟 60fps 摄像头
        cv2, np = self._cv2, self._np
        frame = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
        frame[:, :, 0] = np.linspace(20, 90, self.WIDTH, dtype=np.uint8)[None, :]
        frame[:, :, 1] = np.linspace(30, 60, self.HEIGHT, dtype=np.uint8)[:, None]
        self._frames += 1
        x = int((self._frames * 6) % (self.WIDTH - 60))
        cv2.rectangle(frame, (x, 180), (x + 60, 300), (0, 220, 255), -1)
        cv2.putText(frame, 'FAKE USB CAMERA', (18, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (230, 230, 230), 2)
        cv2.putText(frame, 'frame {}'.format(self._frames), (18, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (160, 220, 255), 2)
        return True, frame

    def release(self):                                # noqa: N802
        self._opened = False


def pump(app, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def pick_database(explicit: str = '', seconds: float = 30.0):
    """挑一个**心电和手环都有数据**的库，返回 `(路径, 窗口起点 epoch 或 None)`。

    锚点不能取「所有表里最早的时间」：摄像头常常比心电早十几分钟开录，按最早时间
    切窗口，窗口里全是摄像头帧，心电/手环一个点都没有，冒烟断言就会误报失败。
    所以锚点取「心电和手环都已经有数据」的那一刻，由调用方用 time_range 精确截段。

    解析不出时间时退回 (路径, None)，调用方按 limit_seconds 从最早期载入。
    """
    if explicit:
        return explicit, None
    candidates = [path for path, _mtime, _size in list_sessions(str(ROOT / 'data'))
                  if path.endswith('.db')
                  # 跳过自检自己产出的库：它是纯摄像头会话，心电/手环全空，
                  # 一旦被选成回放源，下一轮自检就会误报"心电 0 点"。
                  and not os.path.basename(path).startswith('uishot_session_')]
    import sqlite3

    def epoch(text):
        try:
            return datetime.strptime(text, '%Y-%m-%d %H:%M:%S.%f').timestamp()
        except (TypeError, ValueError):
            try:
                return datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timestamp()
            except (TypeError, ValueError):
                return None

    scored = []
    for path in candidates:
        try:
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        except sqlite3.Error:
            continue
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'bracelet_ppg_data' not in tables or 'ecg_data' not in tables:
                continue
            mins = {}
            for table in ('ecg_data', 'bracelet_ppg_data', 'bracelet_gsr_data'):
                if table in tables:
                    mins[table] = conn.execute(
                        'SELECT MIN(time) FROM "%s"' % table).fetchone()[0]
            if not mins.get('ecg_data') or not mins.get('bracelet_ppg_data'):
                continue
            # 锚点 = 两路都有数据的那一刻（后到的那一路的开头）
            start = max(mins['ecg_data'], mins['bracelet_ppg_data'])
            window = '+{} seconds'.format(int(seconds))
            counts = {}
            for table in mins:
                counts[table] = conn.execute(
                    'SELECT COUNT(*) FROM "%s" WHERE time <= datetime(?, ?)' % table,
                    (start, window)).fetchone()[0]
            if not counts.get('ecg_data') or not counts.get('bracelet_ppg_data'):
                continue
            epoch_start = epoch(start)
            score = sum(counts.values())
            scored.append((score, path, epoch_start))
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    if scored:
        scored.sort(reverse=True)
        _score, path, start = scored[0]
        return path, start
    return (candidates[0] if candidates else ''), None


def main() -> int:
    parser = argparse.ArgumentParser(description='界面离屏截图 + 冒烟检查')
    parser.add_argument('--db', default='', help='要回放的库/JSONL')
    parser.add_argument('--seconds', type=float, default=30.0, help='取前多少秒的数据')
    parser.add_argument('--out', default=str(ROOT / 'ui_shots'))
    parser.add_argument('--width', type=int, default=1520)
    parser.add_argument('--height', type=int, default=940)
    args = parser.parse_args()

    # 冒烟只 pump 4 秒 × 3 倍速 ≈ 12 秒的真实数据，按这个窗口挑库
    # 先清掉上一次自检可能遗留的库与媒体目录（进程被超时强杀时走不到收尾），
    # 否则它们会一直被 pick_database 当成候选，或把 data/ 越跑越脏。
    for stale in glob.glob(os.path.join(str(ROOT / 'data'), 'uishot_session_*')):
        try:
            if os.path.isdir(stale):
                import shutil
                shutil.rmtree(stale, ignore_errors=True)
            else:
                os.remove(stale)
        except OSError:
            pass

    db_path, window_start = pick_database(args.db, args.seconds)
    if not db_path or not os.path.exists(db_path):
        print('[失败] 找不到可回放的库，请用 --db 指定')
        return 1
    print(f'回放数据：{db_path}')
    window_range = None
    if window_start is not None:
        # 从「心电与手环都有数据」的那一刻开始截段，避免窗口被前面十几分钟的
        # 纯摄像头帧占满（那样缓冲里永远没有心电/手环点）。
        window_range = (window_start, window_start + args.seconds)
        packets = load_packets(db_path, time_range=window_range)
        print('回放窗口：{} 起 {:.0f} 秒'.format(
            datetime.fromtimestamp(window_start).strftime('%H:%M:%S'), args.seconds))
    else:
        packets = load_packets(db_path, limit_seconds=args.seconds)
    print(f'重建 {len(packets)} 个数据包')

    buffers = LiveBuffers()

    app = create_app(sys.argv)
    # 采集库与回放源分开：假摄像头的帧写进本次自检库，不去污染回放源
    session_db = os.path.join(str(ROOT / 'data'),
                              'uishot_session_{}.db'.format(time.strftime('%H%M%S')))
    window = MonitorWindow(buffers, session_info={'db_path': session_db,
                                                  'source': '回放（自检）'})
    window.resize(args.width, args.height)
    window.show()
    pump(app, 0.4)

    # 设备准备页要显示"已连接"：BLE 状态直接写进登记表，摄像头用假设备真实跑一遍
    window.status.set('ecg', 'connected', 'E1220511000413',
                      'E1220511000413  MTU=247  信号 -52 dBm')
    window.status.set('bracelet', 'connected', 'MPPB20000008',
                      'MPPB20000008  MTU=247')
    window.camera_factory = lambda i, b: FakeCamera(i, b)
    # 走界面同一条启动路径（15fps 固定），而不是在这里写死一个帧率 ——
    # 否则自检测的是"这个脚本的参数"，不是用户点「开始采集」时的真实行为。
    window.start_camera(window._camera_start_config())
    # 用真正的回放器驱动界面（状态页能看到进度条 + 回放预览曲线）
    window.start_replay(db_path, speed=3.0, time_range=window_range)
    pump(app, 4.0)

    snapshot = buffers.snapshot()
    snapshot['rates'] = {'ecg_samples': ECG_RATE, 'ppg_points': 130.0, 'gsr': 5.0}
    for page in window.pages:
        page.on_data(snapshot)
        page.on_tick(snapshot)
    pump(app, 0.5)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    # 三段式流程：先按阶段截，采集段再按标签页逐个截。
    # force=True 是必需的：正常情况下没完成采集进不了②③，但截图要把所有界面都拍到。
    for stage in range(len(STAGE_SPECS)):
        if stage == 2 and not window.review_selection:
            # 正常流程里这一屏是"选完段"之后才来的；截图要模拟一次选区，
            # 否则拍到的是一个没有图的白板
            window.mark_stage_done(1)
            probe = load_packets(db_path, limit_seconds=args.seconds,
                                 time_range=window_range)
            if probe:
                window.review_selection = {
                    'path': db_path, 'start': probe[0][0], 'end': probe[-1][0],
                    'include': None}
        if stage == 3 and window.review_selection:
            # 第 4 段正常是「回放观看 → 下一步」进来的，那时选区已经交给分析页。
            # 直接 goto_stage(force=True) 会把分析页停在未载入状态，拍出来是空表；
            # 走 goto_analysis() 才会 adopt_selection() 并真的算一遍。
            window.goto_analysis()
            pump(app, 1.2)
        else:
            window.goto_stage(stage, force=True)
            # 回放观看页多等一会儿：脉率要攒够几秒脉搏波才算得出来，
            # 等太短的话截图里 PPG 那张还是 --，看不出效果
            pump(app, 3.0 if stage == 2 else 0.4)
        if stage == 0:
            for index in range(window.acq_tabs.count()):
                window.acq_tabs.setCurrentIndex(index)
                pump(app, 0.4)
                page = window.acq_tabs.widget(index)
                title = getattr(page, 'title', window.acq_tabs.tabText(index))
                path = out_dir / f'1_{index + 1}_{title}.png'
                window.grab().save(str(path))
                shots.append(path)
                print(f'已保存 {path}')
        else:
            path = out_dir / f'{stage + 1}_{STAGE_SPECS[stage][0]}.png'
            window.grab().save(str(path))
            shots.append(path)
            print(f'已保存 {path}')

    # ---- 冒烟断言：缓冲里得有东西，心率要能算出来 ----
    snapshot = buffers.snapshot()
    ecg = np.asarray(snapshot['ecg'], dtype=float)
    quality, quality_text = ecg_signal_quality(ecg, ECG_RATE)
    bpm = estimate_heart_rate(ecg, ECG_RATE)
    ppg_points = sum(len(values) for values in snapshot['ppg'].values())
    checks = [
        ('心电缓冲有数据', ecg.size > 0, f'{ecg.size} 点'),
        # 信号过弱/饱和时必须不报心率；信号正常时算法也可能因间期过于不齐而拒报（合理）
        ('信号差时不报心率', quality == 'ok' or bpm is None, f'quality={quality} bpm={bpm}'),
        ('心率在合理范围', bpm is None or 30 <= bpm <= 200, f'bpm={bpm}'),
        ('手环 PPG 有数据', ppg_points > 0, f'{ppg_points} 点'),
        ('GSR 有数据', len(snapshot['gsr']) > 0, f'{len(snapshot["gsr"])} 点'),
    ]
    camera_stats = window.camera_service.stats() if window.camera_service else {}
    camera_rows = 0
    if camera_stats.get('db_path') and os.path.exists(camera_stats['db_path']):
        # 照片落文件后，库里的 jpeg 列没了；用 load_camera_rows 统一取（新旧库都支持）
        from ui.replay import load_camera_rows
        rows = load_camera_rows(camera_stats['db_path'])
        camera_rows = len(rows)
        sample = next((r[2] for r in rows if r[2]), None)
        if sample:
            import cv2
            image = cv2.imdecode(np.frombuffer(sample, dtype=np.uint8), cv2.IMREAD_COLOR)
            checks.append(('落盘的照片能解码', image is not None and image.shape[0] > 0,
                           str(None if image is None else image.shape)))
        checks.append(('照片写进 frames/ 目录',
                       bool(camera_stats.get('media_bytes', 0) > 0),
                       f'{camera_stats.get("media_bytes", 0) / 1024:.0f} KB'))
    # 断言"帧率落在 15fps 附近"，而不是"帧数够多"：后者在 30fps 下也会通过，
    # 等于没测到帧率改动。相机会话时长从 stats 里取，不写死 2.5 秒。
    elapsed = float(camera_stats.get('elapsed') or 0)
    actual_fps = camera_rows / elapsed if elapsed > 0 else 0.0
    checks.append(('摄像头 15fps 入库', camera_rows >= 30 and 12.0 <= actual_fps <= 18.0,
                   f'{camera_rows} 帧 / {elapsed:.1f} 秒 = {actual_fps:.1f} fps'))

    # 第 4 段：分析页必须真的有结果，且皮电这一路要给出明确结论
    # （ok 或 n/a 都算"有结论"，状态为空说明根本没跑到分析）
    analysis = getattr(window, 'analysis_page', None)
    result_rows = analysis.result_table.rowCount() if analysis is not None else 0
    eda_block = ((analysis.last_result or {}).get('皮电GSR') or {}) if analysis else {}
    checks.append(('分析页有结果行', result_rows >= 4, f'{result_rows} 行'))
    checks.append(('皮电块有结论', eda_block.get('状态') in ('ok', 'n/a'),
                   f'状态={eda_block.get("状态")}'))
    if camera_stats:
        print(f'  摄像头实测：{camera_stats.get("actual_fps")} fps，'
              f'入库 {camera_stats.get("written")}')
    window.stop_camera()
    failures = []
    print()
    for name, ok, detail in checks:
        print(f'  {"[OK]" if ok else "[!!]"} {name}（{detail}）')
        if not ok:
            failures.append(name)
    print(f'  信号质量：{quality_text}')
    if bpm:
        print(f'  心率估计：{bpm:.1f} bpm')
    print('\n[通过] 界面渲染与数据链路正常' if not failures
          else f'\n[失败] {failures}')
    window.close()
    # 自检产出的采集库和照片/视频目录要自己收走，否则下一轮 pick_database
    # 可能选中它（纯摄像头会话，心电为空），造成误报。
    # 采集线程刚停时 Windows 还挂着 mp4 / jpg 的句柄，直接删会 PermissionError，
    # 而 rmtree(ignore_errors=True) 会把它吞掉、留下几百张图 —— 所以等一拍再重试。
    import shutil
    frames_dir = media_root(session_db)
    for attempt in range(5):
        for suffix in ('', '-wal', '-shm', '-journal'):
            try:
                os.remove(session_db + suffix)
            except OSError:
                pass
        if not os.path.isdir(frames_dir):
            break
        shutil.rmtree(frames_dir, ignore_errors=True)
        if os.path.isdir(frames_dir):
            time.sleep(0.4)
    if os.path.isdir(frames_dir):
        print(f'[注意] 临时媒体目录未能删除：{frames_dir}')
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
