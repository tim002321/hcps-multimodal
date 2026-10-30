# -*- coding: utf-8 -*-
"""核心层：配置、存储、写库线程、设备状态。

这一层不依赖任何具体设备，也不依赖界面。按需显式导入，不在包初始化时加载子模块
（避免 `python -m core.xxx` 触发重复执行，也避免无关依赖被连带导入）：

    from core.config import Config
    from core.db_storage import SensorDB, DataRecorder
    from core.writer_thread import SensorWriter
    from core.device_status import DeviceStatus

| 模块 | 职责 |
|---|---|
| `config.py` | `config.ini` → 强类型 `Config`；同时定义项目根与 `data/` 的绝对路径 |
| `db_storage.py` | `SensorDB`（建库建表、批量提交）+ `DataRecorder`（按信号分流写表） |
| `writer_thread.py` | `SensorWriter`：单线程消费队列，把解析/入库挪出 BLE 回调 |
| `device_status.py` | `DeviceStatus`：线程安全的设备状态表（BLE 线程写、界面线程读） |
"""
