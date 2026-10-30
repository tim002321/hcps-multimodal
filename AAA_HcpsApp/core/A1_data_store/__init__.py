# -*- coding: utf-8 -*-
"""A1 存储层：DataPacket -> SQLite / 会话文件。

* store.py  SqliteStore（表结构与老库逐字一致）、FileStore（视频/帧/结果图落盘）、
            session_dirs（会话目录规则）、insert_legacy_grip_rows（A2 写回老格式）、
            usable_session_dirs / usable_session_db_path（测试/自检要一个「现在能写」的会话目录）
"""
from core.A1_data_store.store import (FileStore, SqliteStore, insert_legacy_grip_rows,  # noqa: F401
                                      session_dirs, usable_session_db_path, usable_session_dirs)

__all__ = ['FileStore', 'SqliteStore', 'insert_legacy_grip_rows', 'session_dirs',
           'usable_session_dirs', 'usable_session_db_path']
