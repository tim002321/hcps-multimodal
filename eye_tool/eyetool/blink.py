# -*- coding: utf-8 -*-
"""眨眼判定：状态机 + 时长过滤 + 间隔合并。

输入每帧一个"是否闭合"的布尔值（由眼睑开合比相对基线决定），输出眨眼事件。
口径：单次眨眼闭合 20~500 ms，间隔小于 100 ms 的合并为同一次；
超过 500 ms 的不算眨眼，单独记成"长时间闭眼"（打盹/闭目休息）。

两个容易错的点（都踩过）：
* **时长要用"重新睁眼的那一帧"减去"第一次闭眼的那一帧"**。之前用最后一帧
  闭眼时刻，单帧闭眼的时长就成了 0，被下限整段吃掉——实测 45 秒里 8 次闭眼
  全被丢掉，眨眼数恒为 0。
* 30fps 下一次 100 ms 的眨眼只落 3 帧，19fps 时只落 2 帧。所以下限必须是
  1~2 帧的量级（20 ms），不能沿用文献里的 50 ms——那是给 100fps 以上设备写的。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BlinkEvent:
    start: float
    end: float
    closed_frames: int = 0

    @property
    def duration_ms(self):
        return (self.end - self.start) * 1000.0


@dataclass
class BlinkDetector:
    min_duration_ms: float = 40.0
    max_duration_ms: float = 500.0
    merge_gap_ms: float = 100.0
    events: list = field(default_factory=list)

    _in_blink: bool = False
    _start_t: float = 0.0
    _last_close_t: float = 0.0
    _frames: int = 0
    long_closures: int = 0          # 超过 max_duration_ms 的闭眼次数（打盹）

    def update(self, t: float, closed: bool):
        """返回本次新产生的事件（没有则 None）。"""
        if closed:
            if not self._in_blink:
                self._in_blink = True
                self._start_t = t
                self._frames = 0
            self._frames += 1
            self._last_close_t = t
            return None

        if not self._in_blink:
            return None

        # 睁开了：收尾。end 用"重新睁眼这一帧"的时间，而不是最后一帧闭眼的时间，
        # 否则单帧闭眼的时长恒为 0，会被下限全部丢掉
        self._in_blink = False
        start, end = self._start_t, t
        dur_ms = (end - start) * 1000.0
        if dur_ms > self.max_duration_ms:
            self.long_closures += 1     # 闭太久：不是眨眼
            return None
        if dur_ms < self.min_duration_ms:
            return None

        # 与上一个事件太近则合并
        if self.events:
            gap_ms = (start - self.events[-1].end) * 1000.0
            if gap_ms < self.merge_gap_ms:
                self.events[-1].end = end
                self.events[-1].closed_frames += self._frames
                return None

        ev = BlinkEvent(start=start, end=end, closed_frames=self._frames)
        self.events.append(ev)
        return ev

    @property
    def count(self):
        return len(self.events)

    def rate_per_min(self, t: float, window: float = 60.0):
        """最近 window 秒内的眨眼频率（次/分钟）。样本不足时按实际时长折算。"""
        lo = t - window
        recent = [e for e in self.events if e.end >= lo]
        span = min(window, max(1e-6, t))
        if span < 5.0:
            return float('nan')
        return len(recent) * 60.0 / span

    def mean_duration_ms(self):
        if not self.events:
            return float('nan')
        return sum(e.duration_ms for e in self.events) / len(self.events)
