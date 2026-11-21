# -*- coding: utf-8 -*-
"""生成 summary.txt：给人看的一页报告。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from .params import RunParams
from .pipeline import RunStats
from .session import SessionInfo

BOUNDARIES = """\
能力边界（重要）
----------------
1. 68 点来自 dlib 的 iBUG 300-W 模型，人脸框用的是 YOLO 的输出。两者框的边距习惯
   不同，因此下颌 0-16 号点会比真实轮廓外扩 3~8 px。眼、眉、鼻、唇不受影响，
   本工具的 ROI 只用眼和嘴，故不影响产物。
2. EAR = (|p38-p42| + |p39-p41|) / (2|p37-p40|)，单位是无量纲比值，正常睁眼 0.25~0.40。
3. MAR 有两个：inner 用内唇（|62-66| / |60-64|），outer 用外唇（|51-57| / |48-54|）。
   哈欠判定建议 MAR_inner > 0.5 且持续 > 4 秒，单看数值会把说话误判成哈欠。
4. 图片名与采集时的 frames/ 一一对应，可直接用 file_name 与数据库 join。
5. 640x480 下瞳孔直径约 6~10 px，本工具不输出瞳孔直径——该精度不足以做直径测量，
   请看 trends 或改用眼动仪。
"""


def build_summary(info: SessionInfo, stats: RunStats, params: RunParams,
                  output_dir: str, backend: Dict[str, Any],
                  outputs: Dict[str, int]) -> str:
    lines = []
    lines.append('面部 ROI 处理报告')
    lines.append('=' * 48)
    lines.append('生成时间：{}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    lines.append('')
    lines.append('一、输入')
    lines.append('  会话目录：{}'.format(info.session_dir))
    lines.append('  帧目录　：{}'.format(info.frames_dir))
    lines.append('  数据库　：{}'.format(info.db_path or '（无）'))
    if info.probe is not None:
        lines.append('  库结构　：{}'.format(info.probe.describe()))
    lines.append('  帧数　　：{}（{} 段前缀）'.format(info.total, len(info.prefix_counts)))
    if info.width and info.height:
        lines.append('  分辨率　：{}×{}'.format(info.width, info.height))
    if info.duration_s > 0:
        lines.append('  时间跨度：{:.1f} 秒'.format(info.duration_s))
    lines.append('')
    lines.append('二、结果')
    lines.append('  处理帧数：{}'.format(stats.done))
    lines.append('  成功落盘：{}'.format(stats.face_ok))
    lines.append('  无人脸　：{}'.format(stats.no_face))
    lines.append('  关键点异常：{}'.format(stats.implausible))
    lines.append('  文件缺失：{}'.format(stats.missing))
    lines.append('  失败　　：{}'.format(stats.failed))
    lines.append('  跳过（已存在）：{}'.format(stats.skipped))
    lines.append('  耗时　　：{:.1f} 秒（{:.1f} 帧/秒）'.format(stats.elapsed_s, stats.fps))
    if stats.total:
        lines.append('  检出率　：{:.1f}%'.format(100.0 * stats.face_ok / max(stats.total, 1)))
    if stats.cancelled:
        lines.append('  状态　　：被中途停止')
    lines.append('')
    lines.append('三、产物')
    for key, label in (('eye_l', '左眼'), ('eye_r', '右眼'),
                       ('mouth', '嘴部'), ('ann', '68 点自检图')):
        if key in outputs:
            lines.append('  {}：{} 张'.format(label, outputs.get(key, 0)))
    lines.append('  输出目录：{}'.format(output_dir))
    lines.append('')
    lines.append('四、参数')
    lines.append('  检测后端：{}（请求 {}，设备 {}）'.format(
        backend.get('used', '?'), backend.get('requested', '?'), backend.get('device', '?')))
    lines.append('  置信度　：{}　输入尺寸：{}'.format(params.conf, params.imgsz))
    lines.append('  选脸策略：{}（中线带宽 {}）'.format(params.face_pick, params.center_band))
    lines.append('  眼 ROI　：{}×脸宽 / {}×脸高（模式 {}）'.format(
        params.eye_w_ratio, params.eye_h_ratio, params.roi_mode))
    lines.append('  嘴 ROI　：宽×{}、高×{}、中心下移 {}'.format(
        params.mouth_w_scale, params.mouth_h_scale, params.mouth_dy))
    lines.append('  放大倍数：{}x　JPEG 质量：{}'.format(params.roi_scale, params.jpeg_quality))
    lines.append('  参数指纹：{}'.format(params.params_hash()))
    lines.append('')
    lines.append(BOUNDARIES.rstrip())
    lines.append('')
    return '\n'.join(lines)
