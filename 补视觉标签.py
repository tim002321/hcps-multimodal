# -*- coding: utf-8 -*-
"""给历史会话库补视觉标签（vision_frames / yawn_events）。

旧版程序没有把视觉结果落库（`_emit_yawn` 未定义那个 bug），所以 2026-11-10 之前的
9 场会话库里只有原始 JPEG、没有任何推断结果。这个脚本用现有的
「yolov8n-face 人脸 + yawn_model_80 张嘴 + YawnLogic 状态机」把标签补回去，
字段与采集端完全一致，因此回放页和分析页无需改动就能读。

做法
----
1. 按**时间网格**抽帧（默认 2 fps），不是按帧号等距 —— 各场实际帧率 25~30 不一，
   按时间抽才不会在某一段抽得过密或过疏。
2. 逐帧推理后按「墙钟秒」聚合成一行，与 `YawnMonitor._flush_second` 同构。
3. 判定出哈欠时另写一条 `yawn_events`。

用法
----
    python 补视觉标签.py --db <会话库> [--fps 2] [--limit N] [--dry-run]
    python 补视觉标签.py --all [--fps 2]
    python 补视觉标签.py --db <会话库> --resume      # 断点续跑
    python 补视觉标签.py --db <会话库> --force       # 清空重算
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "AAAwindows_code")
sys.path.insert(0, APP)

import numpy as np                                     # noqa: E402
import cv2                                             # noqa: E402


def epoch_of(text: str) -> float:
    """库里存的是 'YYYY-MM-DD HH:MM:SS.ffffff'。"""
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def stamp_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S.%f")


def scan_frames(con):
    """返回 (rowid 数组, epoch 数组)。只读两列，10 万行也很快。"""
    rows = con.execute("SELECT rowid, time FROM camera_frames ORDER BY rowid").fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    ts = np.array([epoch_of(r[1]) for r in rows], dtype=float)
    return ids, ts


def build_schedule(ids, ts, fps: float):
    """返回 [(rowid, 时刻)]。

    fps <= 0 表示**逐帧全跑**（不抽样）。原始视频本身已是抽过帧的
    （标称 30fps，实测 26.8），再抽一次等于二次丢失，哈欠这类
    持续 1~2 秒的事件会受影响。
    """
    if len(ts) < 2:
        return []
    if fps is None or fps <= 0:
        return [(int(i), float(t)) for i, t in zip(ids, ts)]
    step = 1.0 / float(fps)
    grid = np.arange(ts[0], ts[-1], step)
    if grid.size == 0:
        return []
    # 每个网格点找最近的实有帧
    idx = np.searchsorted(ts, grid)
    idx = np.clip(idx, 0, len(ts) - 1)
    left = np.clip(idx - 1, 0, len(ts) - 1)
    choose = np.where(np.abs(ts[left] - grid) <= np.abs(ts[idx] - grid), left, idx)
    # 同一帧被多次选中时只跑一次（视频有断档时会出现）
    keep = np.r_[True, np.diff(choose) > 0]
    return [(int(ids[c]), float(ts[c])) for c in choose[keep]]


class Backfiller:
    def __init__(self, db_path: str, fps: float | None, force: bool, resume: bool,
                 dry_run: bool, limit: int | None, backend: str, device: str):
        self.db_path = db_path
        self.fps = fps
        self.force = force
        self.resume = resume
        self.dry_run = dry_run
        self.limit = limit

        from core.config import Config
        self.cfg = Config.load()

        from vision.face_detector import build_detector
        from vision.mouth_state import MouthClassifier
        from vision.yawn_logic import YawnLogic

        # 人脸走 GPU（大模型，收益大）；张嘴模型很小，放 CPU 反而省掉传输开销
        if device == "cpu":
            face_providers = ["CPUExecutionProvider"]
        else:                                             # gpu / auto
            face_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        self.detector = build_detector(getattr(self.cfg, 'vision_face_detector', 'scrfd'),
                                       conf=self.cfg.vision_face_conf,
                                       providers=face_providers,
                                       backend="ort")
        self.classifier = MouthClassifier(threshold=self.cfg.vision_threshold,
                                          providers=["CPUExecutionProvider"]).load()
        self.logic = YawnLogic(threshold=self.cfg.vision_threshold,
                               yawn_min_sec=self.cfg.vision_yawn_min_sec,
                               cooldown_sec=self.cfg.vision_cooldown_sec,
                               dip_grace_sec=self.cfg.vision_dip_grace)
        self.center_band = self.cfg.vision_center_band
        self.backend_label = "{}|{}".format(
            self.detector.description.split(" / ")[-1], self.classifier.engine)

        self.stats = dict(frames=0, with_face=0, yawns=0, sec_rows=0, prob_sum=0.0)
        self._last_total = 0
        self._acc = self._new_acc()
        self._sec = None
        self._sec_t = None
        self._pending = []

    # ------------------------------------------------------------------
    @staticmethod
    def _new_acc():
        return {"frames": 0, "face_frames": 0, "probs": [], "states": {},
                "conf_max": 0.0, "latency": [], "box": None, "fatigue": "normal"}

    def _flush_second(self, epoch: float | None = None):
        acc, self._acc = self._acc, self._new_acc()
        if epoch is None:
            epoch = self._sec_t if self._sec_t is not None else time.time()
        if not acc["frames"]:
            return
        probs = acc["probs"]
        state = max(acc["states"], key=acc["states"].get) if acc["states"] else "no_face"
        record = {
            "time": float(epoch),
            "has_face": bool(acc["face_frames"]),
            "face_ratio": round(acc["face_frames"] / float(acc["frames"]), 3),
            "face_conf": round(acc["conf_max"], 3),
            "prob_mean": round(float(sum(probs) / len(probs)), 3) if probs else 0.0,
            "prob_max": round(max(probs), 3) if probs else 0.0,
            "state": state,
            "fatigue": acc["fatigue"],
            "yawn_count": int(self._last_total),
            "open_duration": 0.0,
            "box": acc["box"],
            "infer_fps": round(acc.get("fps", 0.0), 1),
            "latency_ms": round(sum(acc["latency"]) / len(acc["latency"]), 1),
            "backend": self.backend_label,
        }
        self._pending.append(("vision_frames", stamp_of(epoch), record))
        self.stats["sec_rows"] += 1

    def _emit_yawn(self, status, prob: float, t: float):
        payload = {
            "time": float(t),
            "prob": round(float(prob), 3),
            "duration": round(float(getattr(status, "open_duration", 0.0) or 0.0), 2),
            "yawn_count": int(getattr(status, "total_count", status.yawn_count) or 0),
            "fatigue": status.fatigue,
            "backend": self.backend_label,
        }
        self._pending.append(("yawn_events", stamp_of(t), payload))
        self.stats["yawns"] += 1

    # ------------------------------------------------------------------
    def run(self) -> dict:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA synchronous=NORMAL")
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "camera_frames" not in tables:
            con.close()
            raise RuntimeError("库里没有 camera_frames")

        ids, ts = scan_frames(con)
        schedule = build_schedule(ids, ts, self.fps)
        if not schedule:
            con.close()
            raise RuntimeError("camera_frames 为空")

        # ---- 断点/重算处理
        start_from = 0
        if self.force:
            for t in ("vision_frames", "yawn_events"):
                con.execute("DROP TABLE IF EXISTS {}".format(t))
            con.commit()
        elif self.resume:
            for t in ("vision_frames", "yawn_events"):
                if t in tables:
                    row = con.execute("SELECT MAX(time) FROM {}".format(t)).fetchone()
                    if row and row[0]:
                        start_from = max(start_from, epoch_of(row[0]))
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        for t in ("vision_frames", "yawn_events"):
            con.execute("CREATE TABLE IF NOT EXISTS {} (time TEXT, value TEXT)".format(t))
            con.execute("CREATE INDEX IF NOT EXISTS idx_{}_time ON {} (time)".format(t, t))
        con.commit()

        if start_from:
            before = len(schedule)
            schedule = [s for s in schedule if s[1] > start_from]
            print("  断点续跑：跳过 %d 个已完成的采样点" % (before - len(schedule)))
        if self.limit:
            schedule = schedule[: self.limit]

        total = len(schedule)
        mode = "逐帧全跑" if (self.fps or 0) <= 0 else "按 %.1f fps 抽帧" % self.fps
        print("  %d 帧 → %s，共 %d 个采样点（%.1f 分钟视频）"
              % (len(ids), mode, total, (ts[-1] - ts[0]) / 60.0))

        started = time.time()
        t_prev = None
        for k, (rid, t) in enumerate(schedule):
            row = con.execute("SELECT jpeg FROM camera_frames WHERE rowid=?",
                              (rid,)).fetchone()
            if not row or not row[0]:
                continue
            frame = cv2.imdecode(np.frombuffer(row[0], np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue

            t0 = time.time()
            boxes = self.detector.detect(frame)
            latency = (time.time() - t0) * 1000.0

            from vision.face_detector import pick_center_face
            main = pick_center_face(boxes, frame.shape[1], self.center_band)
            prob = 0.0
            if main is not None:
                roi = frame[main.y1:main.y2, main.x1:main.x2]
                if roi.size:
                    prob = float(self.classifier.predict(roi))

            # 采样间隔即"帧间隔"，状态机靠它算持续时长。
            # 首帧没有前一帧：全帧模式下按 25fps 估，抽样模式按设定间隔。
            if t_prev is not None:
                dt = t - t_prev
            elif (self.fps or 0) > 0:
                dt = 1.0 / self.fps
            else:
                dt = 0.04
            status = self.logic.update(t, prob, has_face=main is not None)

            if status.total_count > self._last_total:
                self._last_total = status.total_count
                self._emit_yawn(status, prob, t)

            sec = int(t)
            if self._sec is None:
                self._sec = sec
            elif sec != self._sec:
                self._flush_second(self._sec_t)
                self._sec = sec
            acc = self._acc
            acc["frames"] += 1
            if main is not None:
                acc["face_frames"] += 1
                acc["probs"].append(prob)
                acc["conf_max"] = max(acc["conf_max"],
                                      float(getattr(main, "score", 0.0) or 0.0))
                acc["box"] = [int(main.x1), int(main.y1), int(main.x2), int(main.y2)]
            self._sec_t = t
            acc["states"][status.state] = acc["states"].get(status.state, 0) + 1
            acc["fatigue"] = status.fatigue
            acc["latency"].append(latency)
            acc["fps"] = max(1.0 / max(dt, 1e-3), 0.0)
            self.stats["frames"] += 1
            if main is not None:
                self.stats["with_face"] += 1
                self.stats["prob_sum"] += prob
            t_prev = t

            if len(self._pending) >= 200:
                self._write(con)
            if (k + 1) % 200 == 0 or k + 1 == total:
                el = time.time() - started
                rate = (k + 1) / max(el, 1e-6)
                eta = (total - k - 1) / max(rate, 1e-6)
                print("    %5d/%d  %5.1f fps  剩余 %4.0f 分  有脸 %4.1f%%  哈欠 %d"
                      % (k + 1, total, rate, eta / 60.0,
                         100.0 * self.stats["with_face"] / max(self.stats["frames"], 1),
                         self.stats["yawns"]), flush=True)

        if self._sec is not None:
            self._flush_second(self._sec_t)
        self._write(con)

        if not self.dry_run:
            for key, value in (("vision_backfill_at", stamp_of(time.time())),
                               ("vision_backfill_fps", self.fps),
                               ("vision_backfill_sampled", self.stats["frames"]),
                               ("vision_backfill_model", self.backend_label),
                               ("vision_backfill_yawns", self.stats["yawns"])):
                con.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                            (key, str(value)))
            con.commit()
        con.close()
        return self.stats

    def _write(self, con):
        if not self._pending:
            return
        if self.dry_run:
            self._pending.clear()
            return
        by_table = {}
        for table, _st, payload in self._pending:
            by_table.setdefault(table, []).append(
                (stamp_of(payload["time"]), json.dumps(payload, ensure_ascii=False)))
        for table, rows in by_table.items():
            con.executemany(
                "INSERT INTO {} (time, value) VALUES (?, ?)".format(table), rows)
        con.commit()
        self._pending.clear()


def pick_dbs(pattern: str):
    if pattern == "ALL":
        return sorted(p for p in glob.glob(os.path.join(APP, "data", "**", "*.db"),
                                           recursive=True)
                      if os.path.getsize(p) > 1048576)
    return [pattern]


def main():
    ap = argparse.ArgumentParser(description="给历史会话库补视觉标签")
    ap.add_argument("--db", help="会话库路径，或 ALL 表示全部")
    ap.add_argument("--all", action="store_true", help="处理 data/ 下全部会话库")
    ap.add_argument("--fps", type=float, default=0,
                    help="0=逐帧全跑（默认）；>0 按该频率抽帧")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个采样点（调试）")
    ap.add_argument("--force", action="store_true", help="清空已有视觉表重算")
    ap.add_argument("--resume", action="store_true", help="从已有最大时间戳续跑")
    ap.add_argument("--dry-run", action="store_true", help="只推理不写库")
    ap.add_argument("--backend", default="ort",
                    help="人脸后端：ort（默认）/ torch / auto")
    ap.add_argument("--device", default="auto", choices=["auto", "gpu", "cpu"],
                    help="auto=有 CUDA 就用（默认）；gpu/cpu 强制")
    args = ap.parse_args()

    if not args.db and not args.all:
        ap.error("需要 --db <路径> 或 --all")

    targets = pick_dbs("ALL" if args.all else args.db)
    print("待处理 %d 个库" % len(targets))

    grand = dict(frames=0, with_face=0, yawns=0, sec_rows=0, prob_sum=0.0)
    for i, db in enumerate(targets, 1):
        print("\n[%d/%d] %s" % (i, len(targets), os.path.relpath(db, APP)))
        try:
            client = Backfiller(db, args.fps, args.force, args.resume,
                                args.dry_run, args.limit, args.backend, args.device)
            st = client.run()
        except Exception as exc:                           # noqa: BLE001
            print("  失败：%s" % exc)
            continue
        face_pct = 100.0 * st["with_face"] / max(st["frames"], 1)
        mean_prob = st["prob_sum"] / max(st["with_face"], 1)
        print("  完成：%d 帧，有脸 %.1f%%，平均张嘴 %.3f，秒级行 %d，哈欠 %d"
              % (st["frames"], face_pct, mean_prob, st["sec_rows"], st["yawns"]))
        for k in grand:
            grand[k] += st[k]

    if len(targets) > 1:
        print("\n合计：%d 帧，秒级行 %d，哈欠 %d 次，有脸 %.1f%%"
              % (grand["frames"], grand["sec_rows"], grand["yawns"],
                 100.0 * grand["with_face"] / max(grand["frames"], 1)))


if __name__ == "__main__":
    main()
