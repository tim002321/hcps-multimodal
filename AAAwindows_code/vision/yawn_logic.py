# -*- coding: utf-8 -*-
"""打哈欠的时序判定：把逐帧的「张嘴概率」变成「哈欠事件」与疲劳等级。

为什么必须做时序：单帧判张嘴，说话、唱歌、张嘴呼吸全会命中。
文献与工程上的通行做法是「张嘴持续超过 N 秒」才算一次哈欠，再加冷却避免把
一次哈欠拆成多次。本模块就是这条规则，**不依赖任何第三方库**，可单独单测。

四条规则（对应 tools/test_yawn_pipeline.py 的断言）：
  1. 张嘴持续 >= yawn_min_sec       → 计一次哈欠
  2. 只是闪一下（短于 yawn_min_sec） → 不计
  3. 计数后进入 cooldown            → 冷却期内不再重复计
  4. 长时间无人脸                   → 状态回「无脸」，不误计

疲劳等级按「最近 fatigue_window_sec 内的哈欠次数」分级，
阈值（mild_count / severe_count）都可配，换场景要按实测调整。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# 状态常量（界面直接按这个映射文案与颜色）
STATE_NO_FACE = 'no_face'
STATE_NORMAL = 'normal'
STATE_OPEN = 'open'
STATE_YAWNING = 'yawning'

STATE_TEXT = {
    STATE_NO_FACE: '未检测到人脸',
    STATE_NORMAL: '正常',
    STATE_OPEN: '张嘴中',
    STATE_YAWNING: '打哈欠',
}

FATIGUE_NORMAL = 'normal'
FATIGUE_MILD = 'mild'
FATIGUE_SEVERE = 'severe'

FATIGUE_TEXT = {
    FATIGUE_NORMAL: '正常',
    FATIGUE_MILD: '轻度疲劳',
    FATIGUE_SEVERE: '重度疲劳',
}

# 默认参数：都是"初值"，必须用 tools/vision/calibrate_threshold.py 在本地素材上标定
DEFAULT_THRESHOLD = 0.5        # 张嘴概率阈值
DEFAULT_YAWN_MIN_SEC = 1.5     # 连续张嘴多久算一次哈欠
DEFAULT_COOLDOWN_SEC = 5.0     # 一次哈欠后的冷却
DEFAULT_WINDOW_SEC = 600.0     # 疲劳统计窗口（10 分钟）
DEFAULT_MILD_COUNT = 2         # 窗口内 >=2 次 → 轻度
DEFAULT_SEVERE_COUNT = 4       # 窗口内 >=4 次 → 重度
DEFAULT_NO_FACE_SEC = 1.0      # 连续多久没人脸才切到「无脸」态（容忍瞬时漏检）
DEFAULT_DIP_GRACE_SEC = 0.3    # 概率短暂跌回阈值以下多久内不打断「连续张嘴」

# 关于 DIP_GRACE：
# 原来只要有一帧 prob < threshold 就把计时清零。7fps 下「连续 1.5 秒」要有
# 10 帧全部达标，中间任意一帧因为人脸框轻微抖动、被手挡一下、或模型自己在
# 临界值上跳一下，整个哈欠就白打了 —— 这是「识别不灵敏」最主要的原因。
# 给 0.3 秒（约 2 帧）的容忍窗口，既不打断真实哈欠，又不会被 0.5 秒级的
# 说话张嘴蒙混过去（见 tools/test_yawn_pipeline.py 用例 2）。



@dataclass
class YawnStatus:
    """一次更新后的对外快照（界面直接读这个）。"""

    state: str = STATE_NO_FACE
    prob: float = 0.0
    open_duration: float = 0.0        # 本轮已连续张嘴的秒数
    yawn_count: int = 0               # 本会话累计
    recent_count: int = 0             # 统计窗口内的次数
    fatigue: str = FATIGUE_NORMAL
    since_last_yawn: Optional[float] = None

    @property
    def state_text(self) -> str:
        return STATE_TEXT.get(self.state, self.state)

    @property
    def fatigue_text(self) -> str:
        return FATIGUE_TEXT.get(self.fatigue, self.fatigue)


@dataclass
class YawnLogic:
    """有状态的时序判定器。每次推理调用一次 `update()`。"""

    threshold: float = DEFAULT_THRESHOLD
    yawn_min_sec: float = DEFAULT_YAWN_MIN_SEC
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC
    window_sec: float = DEFAULT_WINDOW_SEC
    mild_count: int = DEFAULT_MILD_COUNT
    severe_count: int = DEFAULT_SEVERE_COUNT
    no_face_sec: float = DEFAULT_NO_FACE_SEC
    dip_grace_sec: float = DEFAULT_DIP_GRACE_SEC

    # 内部状态
    events: List[float] = field(default_factory=list)      # 每次哈欠的时刻
    _open_since: Optional[float] = None                    # 本轮张嘴起始时刻
    _dip_since: Optional[float] = None                     # 本轮开始「短暂掉线」的时刻
    _counted_this_run: bool = False                        # 本轮是否已计过
    _last_yawn: Optional[float] = None
    _last_face_at: Optional[float] = None
    _last_seen_face: bool = False
    _prob: float = 0.0

    # ------------------------------------------------------------------
    def update(self, t: float, prob: float, has_face: bool = True) -> YawnStatus:
        """喂一帧的结果。`t` 是单调递增的秒数（用 time.monotonic()）。"""
        self._prob = float(prob) if has_face else 0.0

        # --- 无人脸：先容忍一小段（漏检），超过阈值才真正切态 ---
        if not has_face:
            if self._last_face_at is None or (t - self._last_face_at) >= self.no_face_sec:
                self._open_since = None
                self._dip_since = None
                self._counted_this_run = False
                self._last_seen_face = False
                return self._status(t, STATE_NO_FACE)
            # 容忍期内沿用上一次的判定结果
            return self._status(t, self._state_now(t))

        self._last_face_at = t
        self._last_seen_face = True

        # --- 张嘴判定 ---
        if self._prob >= self.threshold:
            self._dip_since = None                       # 回到阈值以上，容忍期作废
            if self._open_since is None:
                self._open_since = t
                self._counted_this_run = False
            duration = t - self._open_since
            if not self._counted_this_run and duration >= self.yawn_min_sec \
                    and self._cooldown_passed(t):
                self._count_yawn(t)
        elif self._open_since is not None and self.dip_grace_sec > 0:
            # 短暂跌回阈值以下：先不打断，给一个容忍窗口吸收模型抖动/瞬时遮挡。
            # 起因见文件头 DEFAULT_DIP_GRACE_SEC 的说明。
            if self._dip_since is None:
                self._dip_since = t
            if (t - self._dip_since) >= self.dip_grace_sec:
                self._open_since = None
                self._dip_since = None
                self._counted_this_run = False
        else:
            self._open_since = None
            self._dip_since = None
            self._counted_this_run = False

        return self._status(t, self._state_now(t))

    # ------------------------------------------------------------------
    def _cooldown_passed(self, t: float) -> bool:
        return self._last_yawn is None or (t - self._last_yawn) >= self.cooldown_sec

    def _count_yawn(self, t: float) -> None:
        self.events.append(t)
        self._last_yawn = t
        self._counted_this_run = True
        self._prune(t)

    def _prune(self, t: float) -> None:
        cutoff = t - self.window_sec
        if len(self.events) > 1 and self.events[0] < cutoff:
            self.events = [event for event in self.events if event >= cutoff]

    def _in_grace(self, t: float) -> bool:
        """当前是否处在「短暂掉线」的容忍窗口内。"""
        return (self._dip_since is not None and self._open_since is not None
                and (t - self._dip_since) < self.dip_grace_sec)

    def _state_now(self, t: float) -> str:
        if not self._last_seen_face:
            return STATE_NO_FACE
        # 容忍窗口内也算「张嘴中」，否则界面上状态会在 正常/张嘴 之间反复跳
        if self._open_since is not None and (self._prob >= self.threshold
                                             or self._in_grace(t)):
            return STATE_YAWNING if self._counted_this_run else STATE_OPEN
        return STATE_NORMAL

    def _fatigue(self) -> str:
        if len(self.events) >= self.severe_count:
            return FATIGUE_SEVERE
        if len(self.events) >= self.mild_count:
            return FATIGUE_MILD
        return FATIGUE_NORMAL

    def _status(self, t: float, state: str) -> YawnStatus:
        self._prune(t)
        duration = (t - self._open_since) if self._open_since is not None else 0.0
        return YawnStatus(
            state=state,
            prob=self._prob,
            open_duration=round(duration, 2),
            yawn_count=len(self.events),
            recent_count=len(self.events),
            fatigue=self._fatigue(),
            since_last_yawn=round(t - self._last_yawn, 1) if self._last_yawn is not None else None,
        )

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空统计（换被试、重新开始采集时调用）。"""
        self.events.clear()
        self._open_since = None
        self._dip_since = None
        self._counted_this_run = False
        self._last_yawn = None
        self._last_face_at = None
        self._last_seen_face = False
        self._prob = 0.0

    @property
    def threshold_hint(self) -> str:
        return '阈值 {:.2f} / 持续 {:.1f}s'.format(self.threshold, self.yawn_min_sec)
