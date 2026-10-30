# -*- coding: utf-8 -*-
"""A5 对外链路层：跟本机之外的进程/主机打交道，不产生 DataPacket。

* ctrl.py  采集卡主机遥控（SSH）：对时 / 起 app_collect / pkill 停止
* （后续）gpu.py  与推理机的双向报文
"""
from core.A5_link.ctrl import (CollectCardLink, CommandResult, LinkState,  # noqa: F401
                               SshTransport, build_links)

__all__ = ['CollectCardLink', 'CommandResult', 'LinkState', 'SshTransport', 'build_links']
