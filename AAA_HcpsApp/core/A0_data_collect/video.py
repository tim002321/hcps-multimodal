# -*- coding: utf-8 -*-
"""视频采集：起 gstreamer 子进程拉流/录屏，同时把新帧作为数据包发出去。

来源：老工程 data_video.py 的 Data_video_Thread。协议部分照搬：

* 用 `gst-launch-1.0` 跑配置里那条管线：一边 mp4mux 落录像文件，一边 jpegenc 拆帧成 jpg
* 管线里的路径按会话目录替换（老代码是把命令里的 'usr' 换掉，这里支持
  {session} / {frames} / {video} 三个占位符，也兼容老写法的 'usr'）
* Windows 下 gst-launch 路径自带一份：gstreamer/1.0/mingw_x86_64/bin/gst-launch-1.0.exe

相对老代码的改进（行为等价，但更干净）：

* 停止方式：老代码用 ctypes 调 AttachConsole + GenerateConsoleCtrlEvent 给子进程发 Ctrl+C，
  容易把控制台搞乱。新版用 CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT，
  gst-launch 带 -e 参数时会做 EOS，视频能正常收尾；超时再 kill。
* 帧的发现：老代码是界面每 50ms 去磁盘里戳一张 jpg；新版由采集器扫描新帧、
  逐帧 emit 成 DataPacket，界面/推理直接订阅，不再访问磁盘。
* dry_run=True 时不起 gstreamer，只按节奏造帧文件，用于无硬件测试。

写库契约：每帧一行写入新表 data_video_frame（camera / frame_index / path / created_date），
方便离线分析按时间轴取帧（表情识别要用）。
"""
from __future__ import annotations

import glob
import logging
import os
import re
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

from core.A0_data_collect.base import BaseCollector, Sink, now_str

log = logging.getLogger(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VideoCollector(BaseCollector):
    """gstreamer 视频采集器。"""

    name = 'video'
    device = 'video_frame'          # 发出去的是「帧」包，落 data_video_frame 表

    def __init__(self, config: Optional[Dict[str, Any]] = None, sink: Optional[Sink] = None):
        super().__init__(config, sink)
        self.cmd: str = str(self.config.get('cmd', '')).strip()
        self.camera: str = self.config.get('camera', 'video1')
        self.session_dir: str = self.config.get('session_dir', '')
        self.out_dir_name: str = self.config.get('out_dir', 'video1')
        self.frame_pattern: str = self.config.get('frame_pattern', '1_pic_%06d.jpg')
        self.frame_glob: str = self.config.get('frame_glob', '*_pic_*.jpg')
        self.poll_interval: float = float(self.config.get('poll_interval', 0.2))
        self.dry_run: bool = bool(self.config.get('dry_run', False))
        self.gst_path: str = self.config.get('gst_path', '')
        self._proc: Optional[subprocess.Popen] = None
        self._seen: set = set()
        self._dry_index = 0

    # ------------------------------------------------------------- 路径
    @property
    def session_path(self) -> str:
        if self.session_dir:
            return os.path.abspath(self.session_dir)
        return os.path.abspath(os.getcwd())

    @property
    def frames_path(self) -> str:
        return os.path.join(self.session_path, self.out_dir_name)

    def _gst_exe(self) -> str:
        """定位 gst-launch-1.0.exe：配置优先，其次项目内自带，最后交给 PATH。"""
        if self.gst_path:
            path = self.gst_path if os.path.isabs(self.gst_path) else os.path.join(PROJECT_ROOT, self.gst_path)
            if os.path.isfile(path):
                return path
            log.warning('[video] 配置里的 gst_path 不存在：%s', path)
        bundled = os.path.join(PROJECT_ROOT, 'gstreamer', '1.0', 'mingw_x86_64', 'bin', 'gst-launch-1.0.exe')
        if os.path.isfile(bundled):
            return bundled
        return 'gst-launch-1.0'

    def _build_command(self) -> List[str]:
        """把配置里的管线命令做成 argv 列表，并替换路径占位符。"""
        text = self.cmd
        frames = self.frames_path.replace('\\', '/')
        session = self.session_path.replace('\\', '/')
        text = (text.replace('{frames}', frames)
                    .replace('{session}', session)
                    .replace('{video}', os.path.join(session, 'video').replace('\\', '/'))
                    .replace('{camera}', self.camera)
                    .replace('{frame_pattern}', self.frame_pattern))
        if 'usr' in text and '{frames}' not in self.cmd:      # 兼容老配置写法
            text = text.replace('usr', session)
        return [self._gst_exe()] + text.split()

    # ------------------------------------------------------------- 生命周期
    def setup(self) -> None:
        os.makedirs(self.frames_path, exist_ok=True)
        self._seen = {os.path.basename(p) for p in glob.glob(os.path.join(self.frames_path, self.frame_glob))}
        if self.dry_run:
            # 续号：老帧名都已在 _seen 里，再从 1 开始造就等于原地覆盖，
            # 会「写了文件却一个包都不发」（重跑同一个会话目录时的坑）
            self._dry_index = self._next_dry_index()
            log.info('[video] dry_run 模式，不启动 gstreamer，只造帧：%s（从第 %d 帧续）',
                     self.frames_path, self._dry_index + 1)
            return
        if not self.cmd:
            raise RuntimeError('视频管线命令为空，检查配置里的 video.cmd')
        argv = self._build_command()
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        log.info('[video] 启动 gstreamer：%s', ' '.join(argv))
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=creationflags)

    def run_loop(self) -> None:
        while not self.should_stop():
            if self.dry_run:
                self._make_dry_frame()
            elif self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f'gstreamer 进程已退出，返回码 {self._proc.returncode}')
            for frame in self._scan_frames():
                self.emit({'camera': self.camera, 'index': self._frame_index(frame),
                           'path': frame, 'file': os.path.basename(frame), 'send_time': now_str()},
                          raw=None, camera=self.camera)
            self.sleep(self.poll_interval)

    def teardown(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == 'nt':
                proc.send_signal(signal.CTRL_BREAK_EVENT)     # gst-launch -e 会做 EOS，正常收尾
            else:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
            log.info('[video] gstreamer 已优雅退出')
        except Exception:                                     # noqa: BLE001 - 兜底强杀
            log.warning('[video] 优雅停止失败，强制结束进程')
            try:
                proc.kill()
            except Exception:
                pass

    def on_stop(self) -> None:
        self.teardown()

    # ------------------------------------------------------------- 帧处理
    def _scan_frames(self) -> List[str]:
        """按文件名排序返回新出现的帧。"""
        paths = sorted(glob.glob(os.path.join(self.frames_path, self.frame_glob)))
        fresh = [p for p in paths if os.path.basename(p) not in self._seen]
        for p in fresh:
            self._seen.add(os.path.basename(p))
        return fresh

    @staticmethod
    def _frame_index(path: str) -> int:
        match = re.search(r'(\d+)(?=\.\w+$)', os.path.basename(path))
        return int(match.group(1)) if match else 0

    def _make_dry_frame(self) -> None:
        """dry_run：写一个占位 jpg，交给同一个扫描逻辑发现。"""
        self._dry_index += 1
        name = self.frame_pattern % self._dry_index
        with open(os.path.join(self.frames_path, name), 'wb') as fp:
            fp.write(b'\xff\xd8\xff\xe0' + b'dry-run-frame' + b'\xff\xd9')
        time.sleep(0)

    def _next_dry_index(self) -> int:
        """目录里已有的最大帧号（认不出就返回 0，从 1 开始造）。"""
        pattern = re.sub(r'%\d*d', r'(\\d+)', self.frame_pattern)
        regex = re.compile(f'^{pattern}$')
        top = 0
        for path in glob.glob(os.path.join(self.frames_path, self.frame_glob)):
            match = regex.match(os.path.basename(path))
            if match:
                top = max(top, int(match.group(1)))
        return top
