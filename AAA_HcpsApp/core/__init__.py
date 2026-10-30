# -*- coding: utf-8 -*-
"""HcpsApp 核心包。

层级（按前缀排序即依赖顺序）：
    A0_data_collect_*  采集层：设备 -> DataPacket
    A1_data_store_*    存储层：DataPacket -> SQLite
    A2_analyze_*       分析层：数据库 -> 特征/结果表
    A3_ui_*            界面层
    A9_util_*          工具
"""
