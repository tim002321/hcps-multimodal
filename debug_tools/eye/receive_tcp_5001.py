# -*- coding: utf-8 -*-
"""aSeeStudio TCP 眼动/IMU 数据接收（默认连 192.168.2.98:7901）。

## 现场事实（2026-11-17 实测，不是文档推测）

aSeeStudio 那台机器是 **TCP 服务端**：192.168.2.98:7901 处于 LISTEN，客户端一连上
就主动推送数据，不需要先发指令。所以本脚本默认是**客户端**，去连它。

报文形态：**JSON 对象连写，对象之间没有换行/分隔符**，形如
    {"timestamp":1794906758626,"gaze2dX":-1,...,"gyroZ":-1.64,...}{...}{...}
实测 118 Hz、约 73 KB/s、26 个字段、UTF-8、timestamp 为**毫秒**且与本机时钟一致。

字段语义：
    timestamp                                     设备时间（毫秒）
    gaze2dX, gaze2dY                              综合注视点；-1 表示无效
    leftPupil, rightPupil                          瞳孔直径；-1 表示无效
    leftGazeDirectionX/Y/Z, leftGazeOriginX/Y/Z    左眼视线方向与起点
    rightGazeDirectionX/Y/Z, rightGazeOriginX/Y/Z  右眼视线方向与起点
    accelerometerX/Y/Z, gyroX/Y/Z, magnetometerX/Y/Z

用法：
    python receive_tcp_5001.py                        # 连 192.168.2.98:7901
    python receive_tcp_5001.py --host 192.168.2.98 --port 7901
    python receive_tcp_5001.py -o 眼动.csv -r 原始.txt
    python receive_tcp_5001.py --mode listen          # 反过来当服务端（别的设备时用）
    python receive_tcp_5001.py --selftest             # 内置自检，不联网、不写文件

输出两路：
    CSV  一行一帧：本机时间、设备时间(ms) + 上面 25 个字段，共 27 列
    RAW  一行一帧的原始 JSON，便于事后回放/重解析

落盘策略：条数（`--flush-every`，默认 200）与时间（`--flush-interval`，默认 1 秒）
先到者触发，进程被强杀最多丢 1 秒数据。断线自动重连（1→2→5 秒退避）。
按 Ctrl+C 或 Ctrl+Break 正常退出并落盘。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import socket
import struct
import threading
import time
from datetime import datetime

DEFAULT_HOST = '192.168.2.98'
DEFAULT_PORT = 7901
DEFAULT_OUT = '眼动数据_7901.csv'
DEFAULT_ALLOW = '192.168.2.98'

# 新版 JSON 报文的数值字段（顺序固定，CSV 列顺序就按它来）
JSON_KEYS = (
    'gaze2dX', 'gaze2dY', 'leftPupil', 'rightPupil',
    'leftGazeDirectionX', 'leftGazeDirectionY', 'leftGazeDirectionZ',
    'leftGazeOriginX', 'leftGazeOriginY', 'leftGazeOriginZ',
    'rightGazeDirectionX', 'rightGazeDirectionY', 'rightGazeDirectionZ',
    'rightGazeOriginX', 'rightGazeOriginY', 'rightGazeOriginZ',
    'accelerometerX', 'accelerometerY', 'accelerometerZ',
    'gyroX', 'gyroY', 'gyroZ',
    'magnetometerX', 'magnetometerY', 'magnetometerZ',
)
HEADER_JSON = ['本机时间', '设备时间(ms)'] + list(JSON_KEYS)      # 27 列

# 旧版 15 列报文（中文表头，逗号分隔的数组）仍保留解析，便于兼容老固件/老素材
HEADER_LEGACY = [
    "时间戳",
    "推荐眼注视点是否有效", "推荐眼注视点X值", "推荐眼注视点Y值",
    "左眼注视点是否有效", "左眼注视点X值", "左眼注视点Y值",
    "右眼注视点是否有效", "右眼注视点X值", "右眼注视点Y值",
    "左眼瞳孔大小毫米值", "右眼瞳孔大小毫米值",
    "左眼眨眼状态", "右眼眨眼状态", "AOI名称",
]
_INT = re.compile(r"^[+-]?\d+$")

MAX_FRAME = 1 << 20          # 单帧上限 1 MiB，超限说明流已错位
_WS = b" \t\r\n"
_OPEN = (0x5B, 0x7B)         # [ {
_CLOSE = (0x5D, 0x7D)        # ] }
_QUOTE = 0x22                # "

FLAVOR_JSON = 'json'
FLAVOR_LEGACY = 'legacy'


def local_stamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


# ---------------------------------------------------------------------------
# 旧版 15 列解析（数组/逗号文本）
# ---------------------------------------------------------------------------
def _split_top(s: str) -> list[str]:
    """按顶层逗号切分，忽略引号和括号内的逗号。"""
    out, buf, depth, quote = [], [], 0, ""
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in "[{(":
            depth += 1
            buf.append(ch)
        elif ch in "]})":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf or out:
        out.append("".join(buf).strip())
    return out


def _clean(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    return v


def _conv(v: str):
    v = _clean(str(v))
    low = v.lower()
    if low in ("true", "t", "yes"):
        return 1
    if low in ("false", "f", "no", ""):
        return 0
    if _INT.match(v):
        return int(v)
    try:
        return float(v)
    except ValueError:
        return v


def _json_to_parts(obj) -> list[str] | None:
    """旧版：把 JSON 数组/中文键对象转成原始字符串列表。"""
    if isinstance(obj, dict):
        parts = []
        for k in HEADER_LEGACY:
            if k in obj:
                v = obj[k]
                parts.append(json.dumps(v, ensure_ascii=False)
                             if isinstance(v, (list, dict)) else str(v))
        if len(parts) < 14:  # 键名对不上时退回按插入顺序
            parts = [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict))
                     else str(v) for v in obj.values()]
        return parts
    if isinstance(obj, list):
        return [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict))
                else str(v) for v in obj]
    return None


def _row_from_parts(parts) -> list | None:
    if not parts or len(parts) < 14:
        return None
    row = [_conv(p) for p in parts[:14]]
    aoi = parts[14] if len(parts) > 14 else "[]"
    row.append(_clean(str(aoi)))
    return row


def parse_legacy(line: str):
    """旧版 15 列解析；失败返回 None。"""
    s = line.strip()
    if not s:
        return None

    parts = None
    if s[0] in "{[" and s[-1] in "}]":
        try:
            parts = _json_to_parts(json.loads(s))
        except Exception:
            parts = None

    if parts is None:
        # 非标准 JSON 的整体包裹：去掉外层括号再按字段切。
        if len(s) >= 2 and ((s[0] == "[" and s[-1] == "]")
                            or (s[0] == "{" and s[-1] == "}")):
            s = s[1:-1]
        parts = _split_top(s)
    return _row_from_parts(parts)


# ---------------------------------------------------------------------------
# 新版 JSON 帧解析（设备实测格式）
# ---------------------------------------------------------------------------
def is_eye_json(obj) -> bool:
    """是不是设备实测的那套英文字段报文。"""
    return isinstance(obj, dict) and ('gaze2dX' in obj or 'timestamp' in obj)


def json_row(obj: dict, stamp: str = '') -> list:
    """把一条 JSON 帧摊平成 27 列：本机时间、设备时间(ms)、25 个数值字段。"""
    row = [stamp or local_stamp(), obj.get('timestamp', '')]
    for key in JSON_KEYS:
        value = obj.get(key, '')
        row.append('' if value is None else value)
    return row


def gaze_valid(obj: dict) -> bool:
    """注视点是否有效（设备用 -1 表示无效）。"""
    try:
        return float(obj.get('gaze2dX', -1)) != -1
    except (TypeError, ValueError):
        return False


def rows_of(text: str, stamp: str = '') -> tuple[list[list], str | None]:
    """把一条完整帧解析成 (CSV 行列表, 报文类型)。

    类型：'json'（设备实测格式）/ 'legacy'（旧 15 列）/ None（解析失败）。
    """
    s = (text or "").strip()
    if not s:
        return [], None

    if s[0] == '{':
        try:
            obj = json.loads(s)
        except Exception:                             # noqa: BLE001 - 非 JSON 走旧路径
            obj = None
        if is_eye_json(obj):
            return [json_row(obj, stamp)], FLAVOR_JSON
        if isinstance(obj, dict):
            row = _row_from_parts(_json_to_parts(obj))
            if row is not None:
                return [row], FLAVOR_LEGACY

    row = parse_legacy(s)
    if row is not None:
        return [row], FLAVOR_LEGACY
    if "\n" in s or "\r" in s:
        rows = []
        for line in s.splitlines():
            item = parse_legacy(line)
            if item is not None:
                rows.append(item)
        if rows:
            return rows, FLAVOR_LEGACY
    return [], None


# 兼容旧调用名
def parse(line: str):
    rows, _flavor = rows_of(line)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 分帧：TCP 字节流 → 一条条完整报文
# ---------------------------------------------------------------------------
class StreamSplitter:
    """单连接的字节流分帧器。

    设备实测是 `{...}{...}` 连写、无换行，所以 `auto` 用括号配平切分；
    `[]` 数组（旧格式）同样支持。不完整的尾巴留在缓冲里等下次。
    """

    def __init__(self, framing: str = "auto", max_frame: int = MAX_FRAME):
        self.framing = framing
        self.max_frame = max_frame
        self.buf = bytearray()
        self.dropped = 0
        self.desync_events = 0
        self._hinted = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        if self.framing == "line":
            return self._by_line()
        if self.framing == "length":
            return self._by_length()
        return self._by_auto()

    def flush(self) -> list[bytes]:
        rest = bytes(self.buf).strip(_WS)
        self.buf.clear()
        return [rest] if rest else []

    def _by_line(self) -> list[bytes]:
        out = []
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            frame = bytes(self.buf[:i]).strip(_WS)
            del self.buf[:i + 1]
            if frame:
                out.append(frame)
        self._guard()
        return out

    def _by_length(self) -> list[bytes]:
        out = []
        while len(self.buf) >= 4:
            n = struct.unpack(">I", self.buf[:4])[0]
            if n == 0:
                del self.buf[:4]
                continue
            if n > self.max_frame:
                self.dropped += len(self.buf)
                self.desync_events += 1
                self.buf.clear()
                if not self._hinted and (self.desync_events >= 3
                                         or self.dropped >= self.max_frame):
                    self._hinted = True
                    print("[眼动] 长度前缀对不上（已丢弃 {} 字节）：本连接后续帧可能"
                          "全部丢弃；设备实测是 JSON 连写，请改用 -f auto"
                          .format(self.dropped), flush=True)
                break
            if len(self.buf) < 4 + n:
                break
            out.append(bytes(self.buf[4:4 + n]))
            del self.buf[:4 + n]
        return out

    def _by_auto(self) -> list[bytes]:
        out = []
        while True:
            start = 0
            while start < len(self.buf) and self.buf[start] in _WS:
                start += 1
            if start:
                del self.buf[:start]
            if not self.buf:
                return out

            if self.buf[0] in _OPEN:
                n = self._balanced_len()
                if n is None:
                    self._guard()
                    return out
                out.append(bytes(self.buf[:n]))
                del self.buf[:n]
                continue

            i = self.buf.find(b"\n")
            if i < 0:
                self._guard()
                return out
            frame = bytes(self.buf[:i]).strip(_WS)
            del self.buf[:i + 1]
            if frame:
                out.append(frame)

    def _balanced_len(self) -> int | None:
        """第一个「括号配平」片段的长度；还不完整返回 None。

        只把双引号当字符串边界：AOI 名里出现 `O'Brien` 时把 `'` 当引号会卡死计数。
        """
        depth = 0
        in_quote = False
        escaped = False
        for i, ch in enumerate(self.buf):
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == 0x5C:
                    escaped = True
                elif ch == _QUOTE:
                    in_quote = False
                continue
            if ch == _QUOTE:
                in_quote = True
            elif ch in _OPEN:
                depth += 1
            elif ch in _CLOSE:
                depth -= 1
                if depth <= 0:
                    return i + 1
        return None

    def _guard(self) -> None:
        if len(self.buf) > self.max_frame:
            self.dropped += len(self.buf)
            self.buf.clear()


# ---------------------------------------------------------------------------
# 落盘（线程安全）
# ---------------------------------------------------------------------------
class RecordSink:
    """原始 JSON 落 TXT + 解析后落 CSV。

    三种结局互斥：写入成功（ok）、解析失败（bad）、断开时的半帧（partial）。
    表头按第一条成功帧的格式决定（JSON 27 列 / 旧版 15 列），不混存。
    """

    def __init__(self, csv_file, raw_file, flush_every: int,
                 flush_interval: float = 1.0, flavor: str | None = None):
        self._csv = csv_file
        self._raw = raw_file                   # 可为 None：--no-raw 时只写 CSV
        self._writer = csv.writer(csv_file)
        self._flush_every = max(1, int(flush_every))
        self._flush_interval = max(0.1, float(flush_interval))
        self._lock = threading.Lock()
        self.flavor = flavor                 # 'json' / 'legacy' / None（待定）
        self.frames = 0
        self.ok = 0
        self.bad = 0
        self.partial = 0
        self.io_errors = 0
        self.gaze_valid = 0                  # 注视点有效的帧数
        self.flavor_mismatch = 0             # 与文件既有格式不符被丢弃的帧
        self.per_peer: dict[str, list] = {}
        self._pending = 0
        self._last_flush = time.monotonic()
        self._last_io_report = 0.0

    # -- 统计 --------------------------------------------------------------
    def _peer_slot(self, peer: str) -> list:
        key = peer or "未知"
        slot = self.per_peer.get(key)
        if slot is None:
            slot = self.per_peer[key] = [0, 0, 0]
        return slot

    def _io_error(self, where: str, exc: OSError) -> None:
        self.io_errors += 1
        now = time.monotonic()
        if now - self._last_io_report >= 1.0:
            self._last_io_report = now
            print("[眼动] {}写入失败（已 {} 次）：{}".format(where, self.io_errors, exc),
                  flush=True)

    def header_of(self, flavor: str) -> list:
        return HEADER_JSON if flavor == FLAVOR_JSON else HEADER_LEGACY

    def _ensure_flavor(self, flavor: str) -> bool:
        """确定本次写入的列格式；与文件既有格式不符时返回 False。"""
        if self.flavor is None:
            self.flavor = flavor
            try:
                self._writer.writerow(self.header_of(flavor))
                self._csv.flush()
            except OSError as exc:
                self._io_error("表头", exc)
            return True
        if self.flavor != flavor:
            self.flavor_mismatch += 1
            if self.flavor_mismatch == 1:
                print("[眼动] 收到的 {} 报文与文件既有格式（{} 列）不一致，已跳过；"
                      "换一个 -o 文件名可分开存".format(
                          flavor, len(self.header_of(self.flavor))), flush=True)
            return False
        return True

    # -- 写入 --------------------------------------------------------------
    def write(self, data: bytes, peer: str = "") -> None:
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        stamp = local_stamp()
        rows, flavor = rows_of(text, stamp)
        valid = flavor == FLAVOR_JSON and _gaze_ok_from_text(text)
        with self._lock:
            if self._raw is not None:
                try:
                    self._raw.write(data if data.endswith(b"\n") else data + b"\n")
                except OSError as exc:
                    self._io_error("原始文件", exc)
                    return
            self.frames += 1
            slot = self._peer_slot(peer)
            slot[0] += 1
            if not rows or flavor is None:
                self.bad += 1
                slot[2] += 1
                self._pending += 1
                self._maybe_flush()
                return
            if not self._ensure_flavor(flavor):
                slot[2] += 1
                self._pending += 1
                self._maybe_flush()
                return
            for row in rows:
                try:
                    self._writer.writerow(row)
                except OSError as exc:
                    self._io_error("CSV", exc)
                    break
                self.ok += 1
                slot[1] += 1
                if valid:
                    self.gaze_valid += 1
            self._pending += 1
            self._maybe_flush()

    def write_partial(self, data: bytes, peer: str = "") -> None:
        """断开时的残留：只进原始文件，不进 CSV。"""
        if not data:
            return
        with self._lock:
            if self._raw is not None:
                try:
                    self._raw.write(data if data.endswith(b"\n") else data + b"\n")
                except OSError as exc:
                    self._io_error("原始文件", exc)
                    return
            self.frames += 1
            self.partial += 1
            slot = self._peer_slot(peer)
            slot[0] += 1
            slot[2] += 1
            self._pending += 1
            self._maybe_flush()

    def _maybe_flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not (force or self._pending >= self._flush_every
                or now - self._last_flush >= self._flush_interval):
            return
        try:
            if self._raw is not None:
                self._raw.flush()
            self._csv.flush()
        except OSError as exc:
            self._io_error("落盘", exc)
        self._pending = 0
        self._last_flush = now

    def flush(self) -> None:
        with self._lock:
            self._maybe_flush(force=True)

    def totals(self) -> dict:
        with self._lock:
            return {
                'frames': self.frames, 'ok': self.ok, 'bad': self.bad,
                'partial': self.partial, 'io_errors': self.io_errors,
                'gaze_valid': self.gaze_valid,
                'flavor_mismatch': self.flavor_mismatch,
                'flavor': self.flavor,
                'per_peer': {k: list(v) for k, v in self.per_peer.items()},
            }

    def peer_written(self, peer: str) -> int:
        with self._lock:
            slot = self.per_peer.get(peer)
            return int(slot[1]) if slot else 0


def _gaze_ok_from_text(text: str) -> bool:
    """写入前顺带判断这一帧注视点是否有效（避免重复 json.loads）。"""
    try:
        obj = json.loads(text)
    except Exception:                                 # noqa: BLE001
        return False
    return gaze_valid(obj) if is_eye_json(obj) else False


# ---------------------------------------------------------------------------
# 客户端模式：连设备，读流，自动重连
# ---------------------------------------------------------------------------
def run_client(host: str, port: int, sink: RecordSink, stopping: threading.Event,
               framing: str = "auto") -> None:
    label = "{}:{}".format(host, port)
    backoff = 1.0
    connects = 0
    while not stopping.is_set():
        try:
            sock = socket.create_connection((host, port), timeout=5.0)
        except OSError as exc:
            print("[眼动] 连接 {} 失败：{}（{:.0f} 秒后重试）".format(label, exc, backoff),
                  flush=True)
            stopping.wait(backoff)
            backoff = min(backoff * 2, 5.0)
            continue
        connects += 1
        backoff = 1.0
        print("[眼动] 已连接 {}（第 {} 次）".format(label, connects), flush=True)
        splitter = StreamSplitter(framing)
        sock.settimeout(1.0)
        try:
            for level, opt in ((socket.IPPROTO_TCP, socket.TCP_NODELAY),
                               (socket.SOL_SOCKET, socket.SO_KEEPALIVE)):
                try:
                    sock.setsockopt(level, opt, 1)
                except OSError:
                    pass
            while not stopping.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError as exc:
                    print("[眼动] 读取异常：{}".format(exc), flush=True)
                    break
                if not chunk:
                    print("[眼动] 对端关闭了连接", flush=True)
                    break
                for frame in splitter.feed(chunk):
                    sink.write(frame, label)
        finally:
            sink.flush()
            try:
                sock.close()
            except OSError:
                pass
        if stopping.is_set():
            break
        print("[眼动] 2 秒后重连 {}…".format(label), flush=True)
        stopping.wait(2.0)


# ---------------------------------------------------------------------------
# 服务端模式（保留：换别的设备、或设备主动连本机时用）
# ---------------------------------------------------------------------------
def _serve_client(conn: socket.socket, addr, sink: RecordSink,
                  framing: str, stopping: threading.Event) -> None:
    peer = "{}:{}".format(addr[0], addr[1])
    splitter = StreamSplitter(framing)
    got = 0
    print("[眼动] 客户端接入 {}（分帧 {}）".format(peer, framing), flush=True)
    try:
        conn.settimeout(1.0)
        for level, opt in ((socket.IPPROTO_TCP, socket.TCP_NODELAY),
                           (socket.SOL_SOCKET, socket.SO_KEEPALIVE)):
            try:
                conn.setsockopt(level, opt, 1)
            except OSError:
                pass
        while not stopping.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                print("[眼动] {} 读取异常：{}".format(peer, exc), flush=True)
                break
            if not chunk:
                break
            for frame in splitter.feed(chunk):
                sink.write(frame, peer)
                got += 1
        for frame in splitter.flush():
            sink.write_partial(frame, peer)
            got += 1
    finally:
        sink.flush()
        try:
            conn.close()
        except OSError:
            pass
        if splitter.dropped:
            print("[眼动] {} 丢弃 {} 字节（帧超长/流错位 {} 次）".format(
                peer, splitter.dropped, splitter.desync_events), flush=True)
        if splitter.desync_events and sink.peer_written(peer) == 0:
            print("[眼动] {} 一帧都没解析成功（失步 {} 次）：分帧方式很可能不对，"
                  "设备实测是 JSON 连写，请改用 -f auto".format(peer, splitter.desync_events),
                  flush=True)
        print("[眼动] 客户端断开 {}｜本次 {} 条报文".format(peer, got), flush=True)


def _unique_path(path: str) -> str:
    stem, ext = os.path.splitext(path)
    for i in range(2, 12):
        candidate = "{}_{}{}".format(stem, i, ext)
        if not os.path.exists(candidate):
            return candidate
    return "{}_{}{}".format(stem, int(time.time()), ext)


def _detect_flavor(path: str) -> str | None:
    """读已有 CSV 的表头，判断它是 27 列 JSON 还是 15 列旧格式。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            first = handle.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        cells = [c.strip() for c in next(csv.reader([first]))]
    except Exception:                                 # noqa: BLE001
        return None
    if cells == HEADER_JSON:
        return FLAVOR_JSON
    if cells == HEADER_LEGACY:
        return FLAVOR_LEGACY
    return 'unknown'


def _open_files(path, raw_path):
    """打开 CSV 与原始文件；raw_path 为 None 时只开 CSV（--no-raw）。"""
    f = rf = None
    try:
        f = open(path, "a", newline="", encoding="utf-8-sig")
        if raw_path is not None:
            rf = open(raw_path, "ab")
    except OSError as exc:
        print("[眼动] 无法写入文件：{}".format(exc), flush=True)
        if f:
            f.close()
        if rf:
            rf.close()
        return None, None
    return f, rf


def _make_listen_socket(host: str, port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
        srv.listen(8)
    except OSError:
        srv.close()
        raise
    srv.settimeout(1.0)
    return srv


def _run_client_thread(conn, addr, sink, framing, stopping, slots):
    try:
        _serve_client(conn, addr, sink, framing, stopping)
    finally:
        slots.release()


# ---------------------------------------------------------------------------
# 内置自检
# ---------------------------------------------------------------------------
def _selftest() -> int:
    def legacy_frame(aoi: str = "[]") -> bytes:
        return ("[1782367662834217,true,0.64,1.06,true,0.11,0.22,true,0.33,0.44,"
                "3.1,3.2,0,0,{}]".format(aoi)).encode("utf-8")

    eye = ('{"timestamp":1794906758626,"gaze2dX":0.42,"gaze2dY":0.31,'
           '"leftPupil":3.2,"rightPupil":3.3,"leftGazeDirectionX":-1,'
           '"accelerometerX":1.01,"gyroZ":-1.6,"magnetometerY":30.6}').encode("utf-8")

    checks = []

    rows, flavor = rows_of(eye.decode(), 'T')
    checks.append(("设备实测 JSON 能解析成 27 列",
                   len(rows) == 1 and len(rows[0]) == len(HEADER_JSON)
                   and flavor == FLAVOR_JSON))
    checks.append(("JSON 字段按固定列序落位",
                   rows[0][0] == 'T' and rows[0][1] == 1794906758626
                   and rows[0][HEADER_JSON.index('gaze2dX')] == 0.42))
    checks.append(("缺失字段留空而不是报错",
                   rows[0][HEADER_JSON.index('rightGazeOriginZ')] == ''))

    s = StreamSplitter("auto")
    out = s.feed(eye + eye + eye)
    checks.append(("三个 JSON 连写能切成 3 帧", len(out) == 3))

    s = StreamSplitter("auto")
    out = s.feed(eye[:15]) + s.feed(eye[15:])
    checks.append(("JSON 半包跨 recv 边界能拼回", len(out) == 1))

    rows, flavor = rows_of(legacy_frame().decode(), 'T')
    checks.append(("旧版 15 列报文仍能解析", flavor == FLAVOR_LEGACY
                   and len(rows[0]) == 15))

    blank = ('{"timestamp":1794906758626,"gaze2dX":-1,"gaze2dY":-1,'
             '"leftPupil":-1,"rightPupil":-1}').encode("utf-8")
    checks.append(("gaze2dX=-1 判定为无效", _gaze_ok_from_text(blank.decode()) is False))

    s = StreamSplitter("auto")
    s.feed(eye[:20])
    rest = s.flush()
    checks.append(("半帧 flush 交出残片", len(rest) == 1))

    failed = [name for name, ok in checks if not ok]
    print("\n[自检]")
    for name, ok in checks:
        print("  {} {}".format("[通过]" if ok else "[失败]", name))
    print("\n{}".format("[通过] 全部 {} 项".format(len(checks)) if not failed
                        else "[失败] {}".format(failed)))
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# 会话文件夹：把 CSV 直接写进主程序那次采集的目录，事后导入就不用猜库
# ---------------------------------------------------------------------------
def _suggest_session_dir() -> str:
    """默认定位到 data 下最新的会话目录（含非 camera_*.db 的那个）。"""
    base = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "AAAwindows_code"), "data")
    best, best_mtime = "", 0.0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ("frames", "video", "_frames")]
        for name in files:
            if not name.lower().endswith(".db") or name.lower().startswith("camera_"):
                continue
            path = os.path.join(root, name)
            mtime = os.path.getmtime(path)
            if mtime > best_mtime:
                best, best_mtime = root, mtime
    return best or (base if os.path.isdir(base) else "")


def _pick_folder(initial: str = "") -> str:
    """弹窗选文件夹；没有图形环境或没装 tkinter 时返回空串，不抛异常。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:                                     # noqa: BLE001
        return ""
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:                                 # noqa: BLE001 - 部分环境不支持
            pass
        chosen = filedialog.askdirectory(
            title="选择本次采集的会话文件夹（主程序 data/<姓名>/<任务>_<时间>）",
            initialdir=initial or os.getcwd())
        root.destroy()
        return chosen or ""
    except Exception:                                     # noqa: BLE001
        return ""


def _apply_session_dir(args) -> bool:
    """把 --session-dir / --pick-dir 落到 args.out；返回 False 表示该退出。

    文件夹不存在就创建：主程序是「第一次写数据才建库、才 makedirs」，所以采集
    开始前那个目录可能还不存在。提前建好没有副作用（主程序用 exist_ok=True）。
    """
    target = (args.session_dir or "").strip()
    if not target and args.pick_dir:
        target = _pick_folder(_suggest_session_dir())
        if not target:
            print("[眼动] 没能弹出选择框（没有图形界面或缺 tkinter）；"
                  "请改用 --session-dir 指定文件夹", flush=True)
            return False
    if not target:
        return True

    target = os.path.abspath(target)
    if os.path.isabs(args.out):
        print("[眼动] --out 是绝对路径，忽略文件夹选择：{}".format(args.out), flush=True)
        return True
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        print("[眼动] 无法创建文件夹 {}：{}".format(target, exc), flush=True)
        return False

    args.out = os.path.join(target, os.path.basename(args.out))
    print("[眼动] 输出目录：{}".format(target), flush=True)
    try:
        dbs = sorted(n for n in os.listdir(target)
                     if n.lower().endswith(".db") and not n.lower().startswith("camera_"))
    except OSError:
        dbs = []
    if dbs:
        print("[眼动] 该目录已有会话库：{}（导入时按时间窗配对）".format(
            "、".join(dbs[:3])), flush=True)
    else:
        print("[眼动] 提示：该目录还没有会话库 —— 主程序第一次写数据时才建库。"
              "请确认主程序已开始采集，否则这段眼动将来不在任何会话窗口内", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser(description="接收 aSeeStudio 眼动数据（默认客户端模式）")
    ap.add_argument("--mode", choices=("connect", "listen"), default="connect",
                    help="connect=去连设备（实测设备是服务端，默认）；listen=本机当服务端")
    ap.add_argument("--host", default=DEFAULT_HOST, help="设备地址，默认 " + DEFAULT_HOST)
    ap.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                    help="端口，默认 {}".format(DEFAULT_PORT))
    ap.add_argument("--ip", "--bind", dest="ip", default="0.0.0.0",
                    help="listen 模式的绑定地址，默认 0.0.0.0")
    ap.add_argument("--allow", default=DEFAULT_ALLOW,
                    help="listen 模式允许的来源 IP，逗号分隔；any 表示不限")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="输出 CSV 路径")
    ap.add_argument("-r", "--raw", default=None,
                    help="原始数据 TXT 路径，默认与 CSV 同目录同名的 _raw.txt")
    ap.add_argument("-f", "--framing", default="auto", choices=("auto", "line", "length"),
                    help="分帧方式，默认 auto（设备实测是 JSON 连写）")
    ap.add_argument("--max-clients", type=int, default=4, help="listen 模式并发上限，默认 4")
    ap.add_argument("--flush-every", type=int, default=200, help="每 N 条落盘一次，默认 200")
    ap.add_argument("--flush-interval", type=float, default=1.0,
                    help="最长多少秒落盘一次，默认 1.0（与 --flush-every 先到者触发）")
    ap.add_argument("--session-dir", default="",
                    help="把 CSV/原始文件写进这个会话文件夹（主程序 data 下那一个），"
                         "避免事后导入时找不到对应库")
    ap.add_argument("--pick-dir", action="store_true",
                    help="启动时弹窗选择会话文件夹（默认定位到 data 下最新的会话）")
    ap.add_argument("--no-raw", action="store_true",
                    help="只写 CSV，不写原始 JSON（省约 2/3 磁盘）")
    ap.add_argument("--open", action="store_true",
                    help="启动后用默认程序（Excel）打开 CSV，方便实时看")
    ap.add_argument("--selftest", action="store_true", help="跑内置自检后退出（不联网）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not _apply_session_dir(args):
        return 2

    path = os.path.abspath(args.out)
    flavor = None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        existing = _detect_flavor(path)
        if existing in (FLAVOR_JSON, FLAVOR_LEGACY):
            flavor = existing
        else:
            new_path = _unique_path(path)
            print("[眼动] 已有 {} 的表头与当前列不一致，本次改写到 {}".format(
                os.path.basename(path), os.path.basename(new_path)), flush=True)
            path = new_path
    if args.no_raw:
        raw_path = None
    else:
        raw_path = (os.path.abspath(args.raw) if args.raw
                    else os.path.splitext(path)[0] + "_raw.txt")

    for target in (path, raw_path):
        if target is None:
            continue
        folder = os.path.dirname(target)
        if folder:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                print("[眼动] 无法创建目录 {}：{}".format(folder, exc), flush=True)
                return 2

    f, rf = _open_files(path, raw_path)
    if f is None:
        return 2

    sink = RecordSink(f, rf, args.flush_every, args.flush_interval, flavor)
    if flavor is not None:
        print("[眼动] 追加写入（已有 {} 列格式）".format(
            len(sink.header_of(flavor))), flush=True)

    def _where() -> str:
        text = "        CSV -> {}".format(path)
        if raw_path is not None:
            text += "\n        RAW -> {}".format(raw_path)
        return text

    if args.open:
        # 等首帧写进表头再弹，否则 Excel 打开是空文件
        def _popup():
            time.sleep(1.5)
            try:
                os.startfile(path)                       # noqa: S606 - Windows 专用
            except OSError as exc:
                print("[眼动] 无法自动打开 CSV：{}".format(exc), flush=True)
        threading.Thread(target=_popup, name='eye-open', daemon=True).start()

    stopping = threading.Event()

    def _stop(_signum, _frame):
        stopping.set()

    try:
        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass

    srv = None
    threads: list[threading.Thread] = []
    client_thread = None
    try:
        if args.mode == "connect":
            print("[眼动] 客户端模式：连接 {}:{}".format(args.host, args.port), flush=True)
            print("        分帧 -> {}\n{}".format(args.framing, _where()), flush=True)
            client_thread = threading.Thread(target=run_client,
                                             args=(args.host, args.port, sink, stopping,
                                                   args.framing),
                                             name='eye-client', daemon=True)
            client_thread.start()
            while not stopping.is_set():
                stopping.wait(0.5)
        else:
            allowed = None
            allow_text = (args.allow or "").strip()
            if allow_text and allow_text.lower() not in ("any", "*", "all"):
                allowed = {item.strip() for item in allow_text.split(",") if item.strip()}
            srv = _make_listen_socket(args.ip, args.port)
            print("[眼动] 监听 TCP {}:{}".format(args.ip, args.port), flush=True)
            print("        允许来源 -> {}".format(
                "不限" if allowed is None else "、".join(sorted(allowed)) + "（其余拒绝）"),
                flush=True)
            print("        分帧 -> {}\n{}".format(args.framing, _where()), flush=True)
            print("        提示：本机需与发送端同网段，并在防火墙放通入站 TCP {}".format(
                args.port), flush=True)
            slots = threading.Semaphore(max(1, args.max_clients))
            while not stopping.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if stopping.is_set():
                        break
                    print("[眼动] accept 出错：{}".format(exc), flush=True)
                    continue
                if allowed is not None and addr[0] not in allowed:
                    print("[眼动] 拒绝 {}：不在允许名单（{}）".format(
                        addr[0], "、".join(sorted(allowed))), flush=True)
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                if not slots.acquire(blocking=False):
                    print("[眼动] 拒绝 {}：并发连接已达上限 {}".format(
                        addr[0], args.max_clients), flush=True)
                    conn.close()
                    continue
                t = threading.Thread(target=_run_client_thread,
                                     args=(conn, addr, sink, args.framing, stopping, slots),
                                     daemon=True)
                t.start()
                threads = [x for x in threads if x.is_alive()] + [t]
    except OSError as exc:
        print("[眼动] 端口 {} 绑定失败：{}（检查是否被占用/防火墙）".format(args.port, exc),
              flush=True)
        f.close()
        if rf is not None:
            rf.close()
        return 2
    finally:
        if srv is not None:
            srv.close()
        if client_thread is not None:
            client_thread.join(timeout=5.0)
        for t in threads:
            t.join(timeout=5.0)
        alive = sum(1 for t in threads if t.is_alive())
        if alive:
            print("[眼动] 仍有 {} 个客户端线程未退出，强制继续收尾".format(alive), flush=True)
        sink.flush()
        stats = sink.totals()
        f.close()
        if rf is not None:
            rf.close()
        peers = stats['per_peer']
        detail = " / ".join("{} → {} 帧".format(k, v[0]) for k, v in sorted(peers.items()))
        print("[眼动] 结束：收帧 {}，CSV {} 行，解析失败 {}，半帧 {}，写盘错误 {}".format(
            stats['frames'], stats['ok'], stats['bad'], stats['partial'],
            stats['io_errors']), flush=True)
        if stats['flavor'] == FLAVOR_JSON:
            print("        其中注视点有效 {} 帧（其余 gaze2dX=-1，设备未在跟踪）".format(
                stats['gaze_valid']), flush=True)
        if stats['flavor_mismatch']:
            print("        与文件既有格式不符被跳过 {} 帧".format(stats['flavor_mismatch']),
                  flush=True)
        print("        客户端 {} 个{}".format(
            len(peers), "（{}）".format(detail) if detail else ""), flush=True)
        print(_where(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
