#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""眼动报文解析单测（不需要设备、不需要网络、不需要 pyqtgraph）。

    python debug_tools/test_eye_protocol.py

覆盖：
  1. 实测样本：微秒时间戳、归一化坐标（Y 超过 1）、双眼瞳孔、空 AOI
  2. 文档形态：花括号 + 引号包裹的字段
  3. AOI 的三种情况：空数组 / 单个 / 多个
  4. 时间戳三大量纲：微秒 / 毫秒 / 秒，以及字符串时间
  5. 无效帧：null、空串、false
  6. 异常输入：字段不足、空包、非 ASCII 编码、JSON 里混 // 注释
  7. 落库结构：as_row 的键是否齐全
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'AAAwindows_code'))

from devices.eye_protocol import (AOI_KEY, FIELDS, parse_asee,  # noqa: E402
                                  normalize_time)

_results = []

# aSeeStudio 实采的一帧（原样，未改数值）
REAL = ('[1782367662834217, true, 0.645575, 1.0662, true, 0.618701, '
        '0.984101, true, 0.672449, 1.1483, 3.51215, 2.34143, 0, 0, []]')


def check(name: str, condition: bool, detail: str = '') -> None:
    _results.append((name, bool(condition), detail))
    mark = 'OK' if condition else 'FAIL'
    print('  [{}] {}{}'.format(mark, name, ('  ' + detail) if detail and not condition else ''))


def close(a, b, tol=1e-6) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def main() -> int:
    print('=== 1. 实测样本 ===')
    frame = parse_asee(REAL)
    check('能解析', frame is not None)
    if frame is None:
        return 1
    check('字段数 15', frame.field_count == 15, str(frame.field_count))
    # 微秒 → 秒，且不能被当成毫秒（否则会算出 58000 年）
    check('时间戳按微秒解析', close(frame.timestamp, 1782367662.834217, 1e-5),
          str(frame.timestamp))
    year = datetime.fromtimestamp(frame.timestamp).year
    check('年份落在 2026', year == 2026, str(year))
    check('综合注视点', frame.gaze == (0.645575, 1.0662), str(frame.gaze))
    check('有效性为 True', frame.gaze_valid and frame.left_valid and frame.right_valid)
    check('左眼瞳孔', close(frame.left_pupil, 3.51215))
    check('右眼瞳孔', close(frame.right_pupil, 2.34143))
    check('双眼均值', close(frame.pupil, (3.51215 + 2.34143) / 2))
    check('双眼差', close(frame.pupil_asymmetry, 3.51215 - 2.34143))
    check('AOI 为空', frame.aois == (), str(frame.aois))
    # Y = 1.0662 超出 1，仍应判为归一化坐标（视线出屏时正常）
    check('Y 超过 1 仍算归一化', frame.looks_normalized is True)

    print('=== 2. AOI 三种情况 ===')
    one = parse_asee('[1782367662834217,true,0.5,0.5,true,0.5,0.5,true,0.5,0.5,3.5,3.4,0,0,["中心区"]]')
    check('单个 AOI', one is not None and one.aois == ('中心区',), str(one.aois if one else None))
    many = parse_asee('[1782367662834217,true,0.5,0.5,true,0.5,0.5,true,0.5,0.5,3.5,3.4,0,0,'
                      '["左侧区域","中心区","右侧区域"]]')
    check('多个 AOI', many is not None and len(many.aois) == 3, str(many.aois if many else None))
    empty = parse_asee('[1782367662834217,true,0.5,0.5,true,0.5,0.5,true,0.5,0.5,3.5,3.4,0,0,[]]')
    check('空 AOI 不丢帧', empty is not None and empty.aois == ())

    print('=== 3. 文档形态（花括号 + 引号） ===')
    doc = ('{"1782367662834217","true","0.5","0.5","true","0.5","0.5","true",'
           '"0.5","0.5","3.5","3.4","0","0","右侧"}')
    doc_frame = parse_asee(doc)
    check('能解析', doc_frame is not None)
    if doc_frame:
        check('字段数 15', doc_frame.field_count == 15, str(doc_frame.field_count))
        check('AOI 文本形态', doc_frame.aois == ('右侧',), str(doc_frame.aois))
        check('数值正确', close(doc_frame.left_pupil, 3.5))
    notext = ('1782367662834217,true,0.5,0.5,true,0.5,0.5,true,0.5,0.5,3.5,3.4,0,0')
    plain = parse_asee(notext)
    check('无引号无花括号也能解析', plain is not None and plain.field_count == 14)

    print('=== 4. 时间戳量纲 ===')
    check('微秒', close(normalize_time('1782367662834217')[0], 1782367662.834217, 1e-5))
    check('毫秒', close(normalize_time('1782367662834')[0], 1782367662.834, 1e-4))
    check('秒', close(normalize_time('1782367662')[0], 1782367662.0))
    check('字符串带日期', close(normalize_time('2026-11-05 16:36:17.123')[0],
                          datetime(2026, 11, 5, 16, 36, 17, 123000).timestamp(), 1e-3))
    only_time = normalize_time('16:36:17.500')
    check('只有时分秒时补当天', only_time[0] is not None)
    check('认不出来的返回 None', normalize_time('not-a-time')[0] is None)

    print('=== 5. 无效帧 ===')
    invalid = parse_asee('[1782367662834217,false,null,null,false,null,null,false,null,null,'
                         '3.5,3.4,1,0,[]]')
    check('解析成功', invalid is not None)
    if invalid:
        check('无效标记为 False', invalid.gaze_valid is False)
        check('null 坐标变 None', invalid.gaze is None)
        check('瞳孔仍然保留', close(invalid.left_pupil, 3.5))
        check('事件保留原值', invalid.left_event == 1.0, str(invalid.left_event))

    print('=== 6. 异常输入 ===')
    check('字段不足返回 None', parse_asee('{"1","2","3"}') is None)
    check('空串返回 None', parse_asee('') is None)
    check('None 返回 None', parse_asee(None) is None)
    gbk = ('[1782367662834217,true,0.5,0.5,true,0.5,0.5,true,0.5,0.5,3.5,3.4,0,0,["中心区"]]'
           .encode('utf-8'))
    check('UTF-8 字节输入', parse_asee(gbk) is not None)
    gbk_real = ('[1782367662834217,true,0.5,0.5,true,0.5,0.5,true,0.5,0.5,3.5,3.4,0,0,'
                '["中心区"]]').encode('gbk')
    check('GBK 字节输入', parse_asee(gbk_real) is not None)
    annotated = '''[1782367662834217,  // 时间戳(微秒)
 true, 0.645575, 1.0662, true, 0.618701, 0.984101,
 true, 0.672449, 1.1483, 3.51215, 2.34143, 0, 0, []]  // AOI'''
    check('混入 // 注释也能解析', parse_asee(annotated) is not None)

    print('=== 7. 落库结构 ===')
    row = frame.as_row()
    missing = [key for key in FIELDS if key != 'timestamp' and key not in row]
    check('14 个数据字段齐全', not missing, str(missing))
    check('含 AOI 列表', row.get(AOI_KEY) == [])
    check('含设备时间', close(row.get('ts_unix'), 1782367662.834217, 1e-5))
    check('含原始时间串', row.get('ts_raw') == '1782367662834217')
    check('不含 timestamp 键（由 time 列承担）', 'timestamp' not in row)

    failed = [name for name, ok, _ in _results if not ok]
    print()
    print('通过 {}/{}'.format(len(_results) - len(failed), len(_results)))
    if failed:
        print('失败项：')
        for name, _, detail in _results:
            if not _:
                print('  -', name, detail)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
