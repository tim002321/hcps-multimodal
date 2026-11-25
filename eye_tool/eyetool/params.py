# -*- coding: utf-8 -*-
"""默认参数。所有可调项集中在这里，命令行可以覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def find_ffmpeg() -> str:
    """找一个能用的 ffmpeg.exe。优先级：环境变量 > PATH > 常见安装位置。"""
    env = os.environ.get('EYE_FFMPEG')
    if env and os.path.isfile(env):
        return env
    from shutil import which
    hit = which('ffmpeg')
    if hit:
        return hit
    for cand in (r'C:\Users\lenovo\anaconda3\Library\bin\ffmpeg.exe',
                 r'C:\ffmpeg\bin\ffmpeg.exe',
                 r'C:\Program Files\ffmpeg\bin\ffmpeg.exe'):
        if os.path.isfile(cand):
            return cand
    return 'ffmpeg'


def list_devices(ffmpeg: str) -> list:
    """列出 DirectShow 视频设备名。换摄像头后用来核对 device 字段。"""
    import re
    import subprocess
    try:
        out = subprocess.run([ffmpeg, '-hide_banner', '-list_devices', 'true',
                              '-f', 'dshow', '-i', 'dummy'],
                             capture_output=True, text=True, timeout=15).stderr or ''
    except Exception:                                      # noqa: BLE001
        return []
    names = re.findall(r'\[(?:dshow|in#0)[^\]]*\]\s+"([^"]+)"', out)
    # 只有 video 段落里的才是摄像头；音频设备名里通常带“麦克风”
    return [n for n in names if '麦克风' not in n and 'Microphone' not in n.lower()]


@dataclass
class CaptureParams:
    # 这台 4K 自动对焦相机的实测能力（ffmpeg -list_options）：
    #   1920x1080@30 / 2304x1296@30 / 2560x1440@30 / 2688x1512@30 /
    #   3840x2160@30 / 3840x2880@30，MJPG；YUYV 只到 640x480@30、800x600@20。
    # 驱动虽报了 1080p60，实际只交付 30 fps，所以按 30 配。
    device: str = '4K AutoFocus Webcam'
    width: int = 3840
    height: int = 2160
    fps: int = 30
    auto_device: bool = True            # 配置的设备不在时自动挑第一个可用摄像头
    # 管道格式：
    #   mjpeg = 直接传送摄像头原始的 MJPG 码流，Python 侧 cv2.imdecode 解成彩色
    #           （实测 3.5 MB/s、54 fps，解码 4.5 ms；原来灰度 rawvideo 要 102 MB/s）
    #   gray  = ffmpeg 转灰度后传 rawvideo（2 MB/帧，1080p60 时占满管道）
    #   bgr24 = 传彩色 rawvideo（6 MB/帧，实测只有 30 fps，带宽不够）
    pipe: str = 'mjpeg'
    ffmpeg: str = ''                   # 留空自动找


@dataclass
class DetectParams:
    face_model: str = ''               # 留空则用默认相对路径
    face_conf: float = 0.50
    provider: str = 'auto'             # auto / cuda / cpu
    providers: object = None           # 由 resolve_paths 填成实际列表
    eye_pad_w: float = 0.85            # 眼框半宽 / 单眼宽
    eye_pad_h: float = 0.55            # 眼框半高 / 单眼宽
    upscale: int = 2                   # 检测前把眼框放大，改善拟合
    interval: int = 2                  # 每 N 帧做一次人脸检测，中间帧复用
    use_iris: bool = True              # 用 MediaPipe 虹膜锚定法测瞳孔（不可用自动退回阈值法）


@dataclass
class PupilParams:
    # 阈值取 ROI 灰度分位数，比固定阈值更抗光照变化
    dark_percentile: float = 6.0
    min_area_ratio: float = 0.010      # 瞳孔面积 / 眼框面积 下限（小瞳孔很小）
    max_area_ratio: float = 0.60
    min_circularity: float = 0.30     # 仅作参考（轮廓带尾巴会偏低）
    min_aspect: float = 0.45          # 椭圆轴比下限；眼睑压住时会压扁到 0.5 左右
    min_diameter_px: float = 4.0
    # 找不到瞳孔时，用前一帧位置做局部搜索（眨眼时不会乱跳）
    use_last_center: bool = True


@dataclass
class BlinkParams:
    close_ratio: float = 0.45          # （阈值法用）瞳孔直径低于基线这个比例算闭合中
    # （虹膜法用）眼睑开合比 = 上下睑距 / 眼宽。
    # 不写死绝对值：每个人睁眼时的开合比从 0.25 到 0.45 都有，个体差异很大，
    # 所以按"本人基线的百分比"判——低于基线 55% 算闭眼，回到 75% 才算睁开。
    open_ratio_enter: float = 0.45     # 低于 基线×此比例 → 进入闭眼（要够深，浅凹陷是眼睑下垂不是眨眼）
    open_ratio_exit: float = 0.85      # 回到 基线×此比例以上 → 重新睁眼
                                       # 退出阈值必须高：0.72 时一次 3 秒的长闭眼会被
                                       # 反复"睁开—闭上"切成好几段，错记成多次眨眼
    min_duration_ms: float = 40.0
    max_duration_ms: float = 500.0
    merge_gap_ms: float = 100.0


@dataclass
class OutputParams:
    out_dir: str = ''
    save_video: bool = False
    save_snapshot: bool = True


@dataclass
class Params:
    capture: CaptureParams = field(default_factory=CaptureParams)
    detect: DetectParams = field(default_factory=DetectParams)
    pupil: PupilParams = field(default_factory=PupilParams)
    blink: BlinkParams = field(default_factory=BlinkParams)
    output: OutputParams = field(default_factory=OutputParams)

    def resolve_paths(self, here: str):
        """把留空的模型路径补成默认值。here = eye_tool 目录。"""
        if not self.detect.face_model:
            cands = [
                os.path.join(here, 'models', 'det_500m.onnx'),
                os.path.join(here, '..', 'AAAwindows_code', 'models', 'det_500m.onnx'),
            ]
            for c in cands:
                if os.path.isfile(c):
                    self.detect.face_model = os.path.abspath(c)
                    break
        if not self.capture.ffmpeg:
            self.capture.ffmpeg = find_ffmpeg()
        if self.detect.providers is None:
            self.detect.providers = pick_providers(self.detect.provider)
        return self


def pick_providers(mode: str = 'auto'):
    """按可用后端挑 provider 列表。

    实测（RTX 4060 Laptop + det_500m.onnx）：CUDA 5.99 ms / CPU 5.74 ms，
    GPU 并没有优势——模型太小，传输开销吃掉收益。所以默认仍是 CPU，
    把 provider 留成可选项，等换成 RITnet 这类分割网再用。
    """
    want = (mode or 'auto').lower()
    # auto 优先 CUDA：本机 RTX 4060。单帧推理上 GPU 与 CPU 基本持平
    # （9.6 vs 9.9 ms，差在噪声内），但后续换成 RITnet 分割网后 GPU 才会真正发力。
    if want == 'cpu':
        return ['CPUExecutionProvider']
    try:
        import onnxruntime as ort
        avail = ort.get_available_providers()
    except Exception:                                      # noqa: BLE001
        return ['CPUExecutionProvider']
    if 'CUDAExecutionProvider' in avail:
        return ['CUDAExecutionProvider', 'CPUExecutionProvider']
    if want == 'cuda':
        print('[眼动] 本机没有 CUDA provider，退回 CPU')
    return ['CPUExecutionProvider']
