# -*- coding: utf-8 -*-
"""aSeeStudio UDP 字符串流（默认 5001 端口）→ 解码 CSV + 原始数据 TXT。

用法：
    python receive_udp_5001.py
    python receive_udp_5001.py -p 5001 -o 眼动数据.csv -r 眼动原始.txt

默认原始文件为 CSV 同目录同名的 `_raw.txt`（如 `眼动数据_5001_raw.txt`）。
一条报文形如：
    {1782367662834217,true,0.645575,1.0662,true,...,0,0,[]}
值带引号、整体为 JSON 数组/对象、或末尾 AOI 里含逗号，也都能解析。
按 Ctrl+C 正常退出并落盘。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import socket

HEADER = [
    "时间戳",
    "推荐眼注视点是否有效", "推荐眼注视点X值", "推荐眼注视点Y值",
    "左眼注视点是否有效", "左眼注视点X值", "左眼注视点Y值",
    "右眼注视点是否有效", "右眼注视点X值", "右眼注视点Y值",
    "左眼瞳孔大小毫米值", "右眼瞳孔大小毫米值",
    "左眼眨眼状态", "右眼眨眼状态", "AOI名称",
]
_INT = re.compile(r"^[+-]?\d+$")


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
    """把合法 JSON（数组/对象）转成原始字符串列表。"""
    if isinstance(obj, dict):
        parts = []
        for k in HEADER:
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


def parse(line: str):
    """返回 15 列 list；字段不足或空行返回 None。"""
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
        if s[0] == "{" and s[-1] == "}":  # 非标准 JSON 的花括号包法
            s = s[1:-1]
        parts = _split_top(s)

    if len(parts) < 14:
        return None

    row = [_conv(p) for p in parts[:14]]
    aoi = parts[14] if len(parts) > 14 else "[]"
    row.append(_clean(str(aoi)))
    return row


def main():
    ap = argparse.ArgumentParser(description="接收 aSeeStudio UDP 眼动数据并写入 CSV")
    ap.add_argument("-p", "--port", type=int, default=5001, help="UDP 端口，默认 5001")
    ap.add_argument("--ip", default="0.0.0.0", help="绑定地址，默认 0.0.0.0")
    ap.add_argument("-o", "--out", default="眼动数据_5001.csv", help="输出 CSV 路径")
    ap.add_argument("-r", "--raw", default=None,
                    help="原始数据 TXT 路径，默认与 CSV 同目录同名的 _raw.txt")
    ap.add_argument("--flush-every", type=int, default=50, help="每 N 条落盘一次，默认 50")
    args = ap.parse_args()

    path = os.path.abspath(args.out)
    raw_path = os.path.abspath(args.raw) if args.raw else os.path.splitext(path)[0] + "_raw.txt"
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    try:
        sock.bind((args.ip, args.port))
    except OSError as exc:
        print("[眼动] 端口 {} 绑定失败：{}（检查是否被占用/防火墙）".format(args.port, exc),
              flush=True)
        sock.close()
        return
    sock.settimeout(1.0)

    f = rf = None
    try:
        f = open(path, "a", newline="", encoding="utf-8-sig")
        rf = open(raw_path, "ab")  # 按收到的原始字节写，不做编码转换
    except OSError as exc:
        print("[眼动] 无法写入文件：{}".format(exc), flush=True)
        if f:
            f.close()
        if rf:
            rf.close()
        sock.close()
        return

    writer = csv.writer(f)
    if new_file:
        writer.writerow(HEADER)
        f.flush()

    print("[眼动] 监听 UDP {}:{}\n        CSV -> {}\n        RAW -> {}".format(
        args.ip, args.port, path, raw_path), flush=True)
    ok = bad = packets = 0
    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stopping:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            # 原始字节先落盘：即使后面解析失败也不丢原始报文
            rf.write(data if data.endswith(b"\n") else data + b"\n")
            packets += 1
            if packets % max(1, args.flush_every) == 0:
                rf.flush()
            text = data.decode("utf-8", errors="replace")
            for line in text.splitlines() or [text]:
                row = parse(line)
                if row is None:
                    bad += 1
                    continue
                writer.writerow(row)
                ok += 1
            if ok and ok % max(1, args.flush_every) == 0:
                f.flush()
    finally:
        f.flush()
        rf.flush()
        f.close()
        rf.close()
        sock.close()
        print("[眼动] 结束：原始 {} 包，CSV 写入 {} 条，忽略 {} 条\n"
              "        CSV -> {}\n        RAW -> {}".format(
                  packets, ok, bad, path, raw_path), flush=True)


if __name__ == "__main__":
    main()
