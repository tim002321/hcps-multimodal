# -*- coding: utf-8 -*-
"""设备层：每个设备一个模块，统一对外暴露 `start_xxx(...)` 协程。

约定：接收模块只依赖 `core/`，**不依赖 `ui/`**；界面通过 `MonitorRecorder` 挂钩子，
所以同一套接收代码既能被 `main.py`（纯采集）用，也能被 `run_monitor.py`（带界面）用。

按需显式导入，不在包初始化时加载子模块（否则 `python -m devices.camera_receiver`
会重复执行一次，且 `import devices` 会连带要求 bleak / cv2 都装好）：

    from devices.ecg_receiver import PacketOutput, start_receiver
    from devices.bracelet_receiver import start_bracelet
    from devices.camera_receiver import CameraService

| 模块 | 职责 |
|---|---|
| `ecg_receiver.py` | 心电贴：扫描 → 握手 → 订阅 → 244 字节包解码 → `DataRecorder` |
| `bracelet_protocol.py` | 手环协议解析（纯字节，无蓝牙依赖，可离线单测） |
| `bracelet_receiver.py` | 手环：扫描 → 连接 → 订阅 → 复用上面的解析 |
| `camera_receiver.py` | USB 摄像头：按目标帧率截帧、JPEG 入库 |
| `ble_utils.py` | BLE 公共工具：服务表打印、按广播名扫描 |
| `legacy_bracelet.py` | 手环协议独立调试脚本（可执行，主程序不引用） |
"""
