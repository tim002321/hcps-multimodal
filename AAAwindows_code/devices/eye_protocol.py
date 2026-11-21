# -*- coding: utf-8 -*-
"""aSeeStudio 眼动报文解析（纯函数，无 socket 依赖）。

**两种形态都要认** —— 文档口径和实测样本并不完全一致，解析器对两种都做兼容：

1) 实测样本（JSON 数组，一包一帧、无换行）。15 个元素，最后一个为 AOI 数组：

    [1782367662834217, true, 0.645575, 1.0662, true, 0.618701, 0.984101,
     true, 0.672449, 1.1483, 3.51215, 2.34143, 0, 0, []]

   元素含义（按实测样本的注释）：
     1  设备时间戳，**微秒**
     2  综合注视点是否有效（bool）
     3  综合注视点 X（归一化）
     4  综合注视点 Y（归一化，**允许超出 1.0** —— 视线出屏时实测 1.0662）
     5  左眼注视是否有效      6/7   左眼注视点 X/Y
     8  右眼注视是否有效      9/10  右眼注视点 X/Y
     11 左眼瞳孔直径（mm）    12    右眼瞳孔直径（mm）
     13 左眼事件/触发状态（语义未确认，见下）  14 右眼同类
     15 AOI 数组，可为空

2) 文档口径（花括号包裹、引号包裹的字段，逗号分隔）：

    {"时间戳","推荐眼注视点是否有效","推荐眼注视点X值",... ,[AOI名称]}

解析顺序：先按 JSON 解析（实测形态），失败再退回分隔符拆分（文档形态）。

**两处需要特别说明的坑**：

* 时间戳是**微秒**（16 位）。按毫秒算会把 2026 年解析成 58000 年。
  `normalize_time()` 用 1e14 做分界：>1e14 微秒、>1e11 毫秒、>1e8 秒。
* 第 13/14 个字段：厂商文档写的是「左眼/右眼眨眼状态」，但实测样本的注释是
  「事件/触发状态，尚未确认」。两种说法冲突，所以这里**不做 bool 转换、原样保留
  数值**（字段名 `left_event` / `right_event`）。等确认语义后再决定怎么解释，
  现在把 1 当「眨眼」写进分析结果就是给自己埋雷。

使用：

    >>> from devices.eye_protocol import parse_asee
    >>> frame = parse_asee(payload)       # bytes 或 str
    >>> frame.gaze                        # (0.645575, 1.0662)
    >>> frame.pupil                       # 双眼均值
    >>> frame.as_row()                    # 写库用 dict
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 前 14 个定长字段的英文键（顺序即报文顺序，不能改）
FIELDS: Tuple[str, ...] = (
    'timestamp',      # 1  设备时间戳（微秒）
    'gaze_valid',     # 2  综合注视点是否有效
    'gaze_x',         # 3  综合注视点 X
    'gaze_y',         # 4  综合注视点 Y
    'left_valid',     # 5  左眼注视是否有效
    'left_x',         # 6  左眼注视点 X
    'left_y',         # 7  左眼注视点 Y
    'right_valid',    # 8  右眼注视是否有效
    'right_x',        # 9  右眼注视点 X
    'right_y',        # 10 右眼注视点 Y
    'left_pupil',     # 11 左眼瞳孔直径（mm）
    'right_pupil',    # 12 右眼瞳孔直径（mm）
    'left_event',     # 13 左眼事件/触发状态（厂商口径为眨眼状态，未确认）
    'right_event',    # 14 右眼事件/触发状态（同上）
)

# 中文名（界面显示 / 报表表头用；落库用英文键）
FIELD_LABELS: Dict[str, str] = {
    'timestamp': '时间戳（μs）',
    'gaze_valid': '综合注视点是否有效',
    'gaze_x': '综合注视点X值',
    'gaze_y': '综合注视点Y值',
    'left_valid': '左眼注视点是否有效',
    'left_x': '左眼注视点X值',
    'left_y': '左眼注视点Y值',
    'right_valid': '右眼注视点是否有效',
    'right_x': '右眼注视点X值',
    'right_y': '右眼注视点Y值',
    'left_pupil': '左眼瞳孔直径（mm）',
    'right_pupil': '右眼瞳孔直径（mm）',
    'left_event': '左眼事件/眨眼状态（未确认）',
    'right_event': '右眼事件/眨眼状态（未确认）',
}

AOI_KEY = 'aoi'
AOI_LABEL = 'AOI名称'

# AOI 起始下标 = 定长字段数
_AOI_INDEX = len(FIELDS)

# 真 / 假 的常见写法（JSON 给 bool，文档口径给 0/1 或 true/false）
_TRUE_WORDS = frozenset(('1', 'true', 't', 'yes', 'y', 'on', '有效', '是', '闭', '闭眼', 'closed', 'close'))
_FALSE_WORDS = frozenset(('0', 'false', 'f', 'no', 'n', 'off', '无效', '否', '睁', '睁眼', 'open', ''))

# 候选分隔符：逗号是文档口径，其余是现场兜底
_DELIMITERS = (',', '\t', ';', '|')

# 数值字段
_NUMERIC = frozenset(('gaze_x', 'gaze_y', 'left_x', 'left_y', 'right_x', 'right_y',
                      'left_pupil', 'right_pupil'))
# 布尔字段（事件那两个不计入：语义未确认，保留原始值）
_FLAGS = frozenset(('gaze_valid', 'left_valid', 'right_valid'))
# 原样保留数值的字段（不做 bool 转换）
_EVENTS = frozenset(('left_event', 'right_event'))


def to_text(payload: Any) -> str:
    """把 UDP 收到的 payload 转成文本。

    优先 UTF-8（aSeeStudio 的 AOI 名是 UTF-8），失败再试 GBK —— 中文 Windows
    上厂商可能用系统默认编码。最后用 latin-1 兜底，它不抛异常，至少能保留原始
    字节结构，便于排查是哪一段乱码。
    """
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, (bytes, bytearray)):
        return str(payload).strip()
    raw = bytes(payload)
    for encoding in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', errors='replace').strip()


def _cell(value: Any) -> str:
    """把单元格统一成字符串，供下游各解析函数处理。

    JSON 分支拿到的是 bool/int/float/None，文本分支拿到的是 str，
    统一在这里归一，后面的 `_flag` / `_num` 就不用各写一套。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    return str(value).strip()


# ---------------------------------------------------------------------------
# 文本形态：去包装 + 拆字段
# ---------------------------------------------------------------------------
def _strip_wrappers(text: str) -> str:
    """去掉整帧外面那层花括号和换行。

    文档写的是 `{"a","b"}`，真实报文也可能不带花括号；AOI 位置的方括号要留给
    字段自己处理，这里不动 `]`。
    """
    body = text.strip().strip('\r\n')
    if body.startswith('{') and not body.startswith('['):
        body = body[1:]
    if body.endswith('}') and not body.endswith(']'):
        body = body[:-1]
    return body.strip()


def _pick_delimiter(text: str) -> str:
    """挑出现次数最多的候选分隔符；都不出现时按逗号处理。"""
    counts = {d: text.count(d) for d in _DELIMITERS}
    best = max(_DELIMITERS, key=lambda d: counts[d])
    return best if counts[best] > 0 else ','


def split_fields(text: str) -> List[str]:
    """把一帧文本拆成字段列表（兼容带引号 / 不带引号 / 制表符）。

    用 csv 而不是 str.split：字段带双引号时 csv 会自动去掉引号，且能正确处理
    `"a,b",c` 这种引号内含分隔符的情况。
    """
    body = _strip_wrappers(text)
    if not body:
        return []
    delimiter = _pick_delimiter(body)
    try:
        rows = list(csv.reader([body], delimiter=delimiter, quotechar='"'))
    except csv.Error:
        return [part.strip() for part in body.split(delimiter)]
    if not rows:
        return []
    return [cell.strip() for cell in rows[0]]


# ---------------------------------------------------------------------------
# JSON 形态
# ---------------------------------------------------------------------------
_JS_COMMENT = re.compile(r'//[^\n]*')


def _loads_lenient(body: str) -> Optional[Any]:
    """尽量把文本当 JSON 读。

    先直接 loads；失败再去掉 `//` 行注释和尾随逗号重试 —— 现场有人会把
    aSeeStudio 的输出连注释一起贴进配置或日志里，直接报错太脆。
    """
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        pass
    cleaned = _JS_COMMENT.sub('', body)
    cleaned = re.sub(r',\s*([\]}])', r'\1', cleaned)
    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        return None


def _from_json(text: str) -> Optional[List[Any]]:
    """把 JSON 形态的报文转成字段列表；不是 JSON 返回 None。"""
    body = text.strip()
    if not body or body[0] not in '[{':
        return None
    parsed = _loads_lenient(body)
    if isinstance(parsed, dict):
        # {"a":1,...} 这种对象形态，按值顺序取（不指望键名，键名不可控）
        parsed = list(parsed.values())
    if not isinstance(parsed, list):
        return None
    return parsed


# ---------------------------------------------------------------------------
# 单字段归一化
# ---------------------------------------------------------------------------
def _flag(text: str) -> Optional[bool]:
    """有效性解析成布尔。认不出来的返回 None，不猜。"""
    value = (text or '').strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    return None


def _num(text: str) -> Optional[float]:
    """解析数值。空串、'nan'、'inf'、乱码一律当作没有值。"""
    value = (text or '').strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number in (float('inf'), float('-inf')):   # NaN / ±Inf
        return None
    return number


def _event(text: str) -> Optional[float]:
    """事件/触发状态：**保留原始数值**，不转 bool。

    厂商文档叫「眨眼状态」，实测样本注释叫「事件/触发状态（尚未确认）」。
    语义没定之前，把 1 直接当「闭眼」会污染后续眨眼频率统计，所以只存原值。
    """
    return _num(text)


_TIME_FORMATS = (
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y/%m/%d %H:%M:%S.%f',
    '%Y/%m/%d %H:%M:%S',
    '%H:%M:%S.%f',
    '%H:%M:%S',
)

# 时间戳量纲分界。实测设备发的是微秒（1782367662834217，16 位）：
# 按毫秒算会把它解释成 58000 年，所以必须单独认这一档。
_US_THRESHOLD = 10 ** 14
_MS_THRESHOLD = 10 ** 11
_SEC_THRESHOLD = 10 ** 8


def normalize_time(raw: str) -> Tuple[Optional[float], str]:
    """时间戳归一化为 (unix 秒, 原始串)。

    认这几种：
      * 微秒（> 1e14，aSeeStudio 实测口径）→ 除 1e6，用整数除法保住微秒精度
      * 毫秒（> 1e11）→ 除 1e3
      * 秒（> 1e8）→ 原样
      * 相对时间 / 帧序号（小数值）→ 原样，由调用方判断
      * `2026-11-05 16:36:17.123` / `16:36:17.123` → 按本地时间解析，后者补当天日期

    认不出来返回 (None, 原始串) —— 宁可让下游看到「没有可信时间」，也不编一个。
    """
    text = (raw or '').strip()
    if not text:
        return None, ''

    try:
        as_int = int(text)
    except ValueError:
        as_int = None

    if as_int is not None:
        if as_int > _US_THRESHOLD:                       # 微秒
            return as_int // 1_000_000 + (as_int % 1_000_000) / 1e6, text
        if as_int > _MS_THRESHOLD:                       # 毫秒
            return as_int / 1e3, text
        if as_int > _SEC_THRESHOLD:                      # 秒
            return float(as_int), text
        return float(as_int), text                       # 相对时间，原样

    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return (None if number != number else number), text

    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt.startswith('%H'):
            today = datetime.now()
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
        return parsed.timestamp(), text
    return None, text


def _clean_aoi(cell: Any) -> Optional[str]:
    """清理单个 AOI 名称：去方括号、去引号、去空白；空的不算。"""
    name = _cell(cell).strip('"').strip("'").strip()
    if name.startswith('['):
        name = name[1:]
    if name.endswith(']'):
        name = name[:-1]
    name = name.strip().strip('"').strip("'").strip()
    return name or None


# ---------------------------------------------------------------------------
# 帧对象
# ---------------------------------------------------------------------------
@dataclass
class EyeFrame:
    """一帧眼动数据。数值字段解析不了就是 None，不做零填充。"""

    timestamp: Optional[float] = None          # 归一化后的 unix 秒；相对时间则原样
    timestamp_raw: str = ''
    gaze_valid: Optional[bool] = None
    left_valid: Optional[bool] = None
    right_valid: Optional[bool] = None
    gaze_x: Optional[float] = None
    gaze_y: Optional[float] = None
    left_x: Optional[float] = None
    left_y: Optional[float] = None
    right_x: Optional[float] = None
    right_y: Optional[float] = None
    left_pupil: Optional[float] = None
    right_pupil: Optional[float] = None
    left_event: Optional[float] = None         # 语义未确认，保留原值
    right_event: Optional[float] = None
    aois: Tuple[str, ...] = ()
    field_count: int = 0                       # 实际拆出多少个字段（联调时看这个）
    raw: str = field(default='', repr=False)

    # ------------------------------------------------------------------
    @property
    def gaze(self) -> Optional[Tuple[float, float]]:
        """综合注视点；任一分量为空时返回 None（半个坐标没有意义）。"""
        if self.gaze_x is None or self.gaze_y is None:
            return None
        return self.gaze_x, self.gaze_y

    @property
    def left_gaze(self) -> Optional[Tuple[float, float]]:
        if self.left_x is None or self.left_y is None:
            return None
        return self.left_x, self.left_y

    @property
    def right_gaze(self) -> Optional[Tuple[float, float]]:
        if self.right_x is None or self.right_y is None:
            return None
        return self.right_x, self.right_y

    @property
    def pupil(self) -> Optional[float]:
        """瞳孔直径（mm）双眼均值。

        实测两眼差异可以很大（3.51 / 2.34），均值只作概览用；
        要做瞳孔指标请分开看 left_pupil / right_pupil。
        """
        values = [v for v in (self.left_pupil, self.right_pupil) if v is not None]
        return sum(values) / len(values) if values else None

    @property
    def pupil_asymmetry(self) -> Optional[float]:
        """双眼瞳孔差（左 − 右，mm）。两眼都有值才算。"""
        if self.left_pupil is None or self.right_pupil is None:
            return None
        return self.left_pupil - self.right_pupil

    @property
    def blinking(self) -> bool:
        """任一眼事件标志非 0 即视为「有事件」。

        注意：这是**推测**——字段语义未确认（见模块 docstring）。眨眼频率这类
        结论暂时不要基于它，等厂商确认 13/14 字段含义后再用。
        """
        return bool(self.left_event) or bool(self.right_event)

    @property
    def looks_normalized(self) -> bool:
        """注视点是否像归一化坐标（0~1 尺度）。

        留了余量：实测样本的 Y 是 1.0662，视线出屏时正常会略超出 1，
        所以按 -0.5~1.5 判断，而不是死卡 0~1。只作提示，不改数据 ——
        到底怎么解释得看现场标定，不能替用户决定。
        """
        points = [p for p in (self.gaze, self.left_gaze, self.right_gaze) if p]
        if not points:
            return False
        return all(-0.5 <= v <= 1.5 for point in points for v in point)

    # ------------------------------------------------------------------
    def as_row(self) -> Dict[str, Any]:
        """落库用的扁平 dict（英文键 + aoi 列表）。

        设备时间戳单独放 ts_unix（秒，保留三位），不占 time 列 —— time 列写的是
        本机接收时间，与其它传感器同一时钟，回放才能对齐。
        """
        row: Dict[str, Any] = {key: getattr(self, key) for key in FIELDS if key != 'timestamp'}
        row[AOI_KEY] = list(self.aois)
        if self.timestamp is not None:
            row['ts_unix'] = round(float(self.timestamp), 6)
        row['ts_raw'] = self.timestamp_raw
        return row

    def summary(self) -> str:
        """一行摘要，供联调脚本 / 状态栏打印。"""
        parts = []
        if self.gaze:
            parts.append('综合({:.3f},{:.3f})'.format(*self.gaze))
        if self.pupil is not None:
            parts.append('瞳孔{:.2f}mm'.format(self.pupil))
        if self.blinking:
            parts.append('事件{}/{}'.format(
                int(self.left_event or 0), int(self.right_event or 0)))
        if self.aois:
            parts.append('AOI:' + '/'.join(self.aois))
        return ' '.join(parts) or '（无有效值）'


def frame_from_cells(cells: Sequence[Any], raw: str = '') -> Optional[EyeFrame]:
    """从已拆好的字段列表构造一帧；字段不够（< 14）返回 None。

    AOI 可有可无 —— 实测样本里它就是空数组 `[]`，不能因为这一帧没有 AOI
    就丢掉整帧数据。
    """
    if len(cells) < _AOI_INDEX:
        return None

    frame = EyeFrame(raw=raw, field_count=len(cells))
    for index, key in enumerate(FIELDS):
        cell = _cell(cells[index])
        if key == 'timestamp':
            frame.timestamp, frame.timestamp_raw = normalize_time(cell)
        elif key in _NUMERIC:
            setattr(frame, key, _num(cell))
        elif key in _FLAGS:
            setattr(frame, key, _flag(cell))
        elif key in _EVENTS:
            setattr(frame, key, _event(cell))
        else:                                   # 理论上不会走到，留个出口
            setattr(frame, key, cell)

    aois: List[str] = []
    for cell in cells[_AOI_INDEX:]:
        if isinstance(cell, (list, tuple)):      # JSON 形态：AOI 直接是数组
            for item in cell:
                name = _clean_aoi(item)
                if name:
                    aois.append(name)
            continue
        # 文本形态：可能整段挤在 `[a,b]` 里，先拆再清
        for piece in re.split(r'[,;|]', _cell(cell)):
            name = _clean_aoi(piece)
            if name:
                aois.append(name)
    frame.aois = tuple(aois)
    return frame


def parse_asee(payload: Any) -> Optional[EyeFrame]:
    """解析一帧 aSeeStudio 报文；格式对不上返回 None。

    接收端唯一要调的入口：字节 → 文本 → （JSON 优先 / 分隔符兜底）→ 归一化。
    """
    text = to_text(payload)
    if not text:
        return None
    cells = _from_json(text)
    if cells is None:
        cells = split_fields(text)
    return frame_from_cells(cells, raw=text)
