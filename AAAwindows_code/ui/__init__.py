# -*- coding: utf-8 -*-
"""界面层（PyQt5 + pyqtgraph）。按需显式导入，不在包初始化时加载主窗口
（否则 `import ui.replay` 也会把整个 GUI 拉起来，离线分析脚本没必要装 PyQt5 全套）。

| 模块 | 职责 |
|---|---|
| `base.py` | `BasePage` / `ChannelGridPage`：页面基类与「曲线网格 + 参数选择」 |
| `theme.py` | 配色与全局 QSS、字体与素材路径 |
| `widgets.py` | 卡片 / 数值块 / 导航项 / 曲线画布 / 参数双列表 |
| `buffers.py` | 线程安全的实时缓冲 + `MonitorRecorder`（入库钩子） |
| `replay.py` | 历史库 / JSONL 回放 |
| `pages_extra.py` | 会话与回放 / 状态识别 / 数据分析 / 预留设备页 |
| `profile.py` | 登录 / 被试信息录入（人物名、组别、实验时间、服务器 IP） |
| `window.py` | 主窗口骨架、两级导航，以及采集侧六个页面（设备准备/心电/手环/摄像头/肌电/眼动） |
"""
