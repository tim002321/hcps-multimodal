# -*- coding: utf-8 -*-
"""A5 对外链路：采集卡主机遥控（SSH）。

来源：老工程 collect_control_thread.py 的 Collect_Thread。老代码只干三件事：

1. 对时：把本机时间写到采集卡主机    sudo date -s '<MM/DD/YYYY HH:MM:SS>'
2. 启动：进工作目录起采集程序        cd <workdir> && sudo nohup <program> > <log_file>
3. 停止：按进程名杀                  sudo pkill -f app_collect / sudo pkill -f gst-launch-1.0

老代码里被写死、这里提到配置的东西（见 config/default.yaml 的 links.ctrl）：

* 用户名/密码（老代码里是两处硬编码）
* 工作目录 /home/rock/data_aggregation_1/dist/app_collect
* 进程名 app_collect / gst-launch-1.0
* sudo 密码靠 stdin.write 写死

行为保持等价：命令串、对时格式（%m/%d/%Y）、sudo 喂密码方式（pty + 往 stdin 写一行）
都和老的逐字一致；对时/启动/停止之外不加任何「顺手改一下」的额外命令。

契约：这条链路**不产生 DataPacket、不写库**，所以不归 A0/A1 管；
它只跟会话一起被启停（CollectorManager.add_link），或者被界面单独调用。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class LinkState(str, Enum):
    """链路状态。取值口径和 A0 的 CollectorState 一致，方便总控/界面统一显示。"""

    IDLE = 'idle'
    CONNECTING = 'connecting'
    RUNNING = 'running'
    ERROR = 'error'
    STOPPED = 'stopped'


@dataclass
class CommandResult:
    """一条远端命令的执行结果，按顺序记进 CollectCardLink.history。"""

    command: str
    exit_status: Optional[int] = None
    output: str = ''
    error: str = ''
    duration: float = 0.0
    password_fed: bool = False

    @property
    def ok(self) -> bool:
        # 起 nohup 采集程序的通道可能一直不退出，取不到退出码（None）也算成功
        return not self.error and self.exit_status in (None, 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'command': self.command,
            'exit_status': self.exit_status,
            'ok': self.ok,
            'error': self.error,
            'duration': round(self.duration, 3),
            'password_fed': self.password_fed,
            'output': self.output[:200],
        }


class SshTransport:
    """paramiko 薄封装：连接、带 pty 执行命令、往 stdin 喂 sudo 密码、关闭。

    这一类只负责「传输」，不认识 date / pkill 这些业务命令，
    所以测试里可以整个换成假的（见 tools/test_ctrl_link.py）。
    """

    def __init__(self, accept_unknown_host: bool = True, default_timeout: float = 5.0):
        self.accept_unknown_host = bool(accept_unknown_host)
        self.default_timeout = float(default_timeout)
        self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self, host: str, port: int = 22, user: str = '', password: str = '',
                timeout: float = 10.0) -> 'SshTransport':
        try:
            import paramiko
        except ImportError as exc:                      # pragma: no cover - 环境问题
            raise RuntimeError('未安装 paramiko：pip install paramiko') from exc

        client = paramiko.SSHClient()
        # 老代码就是 AutoAddPolicy：采集卡主机固定一台，不折腾 known_hosts
        policy = paramiko.AutoAddPolicy() if self.accept_unknown_host else paramiko.RejectPolicy()
        client.set_missing_host_key_policy(policy)
        log.info('[ctrl] 连接采集卡主机 %s@%s:%s', user or '<默认用户>', host, port)
        client.connect(hostname=host, port=int(port), username=user or None,
                       password=password or None, timeout=float(timeout))
        self._client = client
        return self

    def exec(self, command: str, password: Optional[str] = None,
             timeout: Optional[float] = None) -> CommandResult:
        """带 pty 执行一条命令；password 非空时往 stdin 写一行（喂 sudo 提示）。"""
        client = self._client
        if client is None:
            raise RuntimeError('SSH 未连接')
        timeout = float(timeout or self.default_timeout)

        result = CommandResult(command=command)
        stdin, stdout, _stderr = client.exec_command(command, get_pty=True, timeout=timeout)
        if password:
            stdin.write(password + '\n')
            stdin.flush()
            result.password_fed = True
        try:
            stdin.channel.shutdown_write()
        except Exception:                               # noqa: BLE001 - 关不关得掉都不影响命令
            log.debug('[ctrl] shutdown_write 失败', exc_info=True)

        result.output = self._read_channel(stdout.channel, timeout)
        result.exit_status = self._exit_status(stdout.channel)
        return result

    @staticmethod
    def _read_channel(channel, timeout: float) -> str:
        """小块读，超时就把已经拿到的返回。

        起 nohup 采集程序时通道可能一直不关（远端程序在跑），不能死等 read()。
        """
        import socket

        data = bytearray()
        deadline = time.time() + timeout
        channel.settimeout(0.2)
        while time.time() < deadline:
            try:
                chunk = channel.recv(4096)
            except socket.timeout:
                continue
            except Exception:                           # noqa: BLE001 - 通道被远端关掉
                break
            if not chunk:
                break
            data.extend(chunk)
        return data.decode('utf-8', 'ignore')

    @staticmethod
    def _exit_status(channel) -> Optional[int]:
        try:
            if channel.exit_status_ready():
                return int(channel.recv_exit_status())
        except Exception:                               # noqa: BLE001 - 取不到就算了
            log.debug('[ctrl] 取退出码失败', exc_info=True)
        return None

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:                           # noqa: BLE001
                log.debug('[ctrl] 关闭 SSH 失败', exc_info=True)


class CollectCardLink:
    """采集卡主机遥控链路（对应老 Collect_Thread）。

    用法：
        link = CollectCardLink.from_config(yaml.safe_load(open('config/default.yaml')))
        link.start()          # = 连接 + 对时 + 起 app_collect（老 run()）
        ...
        link.stop()           # = pkill 掉远端采集程序 + 关 SSH
    """

    name = 'ctrl'

    def __init__(self, config: Optional[Dict[str, Any]] = None, transport: Any = None):
        self.config: Dict[str, Any] = dict(config or {})

        self.host: str = str(self.config.get('host') or '')
        self.port: int = int(self.config.get('port', 22))
        self.user: str = str(self.config.get('user') or '')
        self.password: str = str(self.config.get('password') or '')
        self.connect_timeout: float = float(self.config.get('connect_timeout', 10.0))
        self.read_timeout: float = float(self.config.get('read_timeout', 5.0))
        self.accept_unknown_host: bool = bool(self.config.get('accept_unknown_host', True))

        self.use_sudo: bool = bool(self.config.get('use_sudo', True))
        self.sync_time_on_start: bool = bool(self.config.get('sync_time', True))
        self.workdir: str = str(self.config.get('workdir') or '')
        self.program: str = str(self.config.get('program') or '')
        self.log_file: str = str(self.config.get('log_file') or '/dev/null')
        patterns = self.config.get('stop_patterns') or []
        if isinstance(patterns, str):
            patterns = [patterns]
        self.stop_patterns: List[str] = [str(p) for p in patterns if str(p).strip()]
        self.delay_after_start: float = float(self.config.get('delay_after_start', 1.0))

        self.transport = transport if transport is not None else SshTransport(
            self.accept_unknown_host, self.read_timeout)
        self.state: LinkState = LinkState.IDLE
        self.last_error: str = ''
        self.history: List[CommandResult] = []

    # ------------------------------------------------------------- 构造
    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None,
                    transport: Any = None) -> 'CollectCardLink':
        """config 给整份 default.yaml 或只给 links.ctrl 那一段都行。"""
        section: Any = config or {}
        if isinstance(section, dict) and isinstance(section.get('links'), dict):
            section = (section['links'] or {}).get(cls.name) or {}
        return cls(section, transport=transport)

    @property
    def target(self) -> str:
        return f'{self.user}@{self.host}:{self.port}' if self.user else f'{self.host}:{self.port}'

    @property
    def running(self) -> bool:
        return self.state in (LinkState.RUNNING, LinkState.CONNECTING)

    # ------------------------------------------------------------- 命令拼装
    def _sudo(self, command: str) -> str:
        return f'sudo {command}' if self.use_sudo else command

    def sync_command(self, now: Optional[datetime] = None) -> str:
        """对时命令。老代码格式：sudo date -s '09/15/2026 15:20:30'。"""
        now = now or datetime.now()
        return self._sudo(f"date -s '{now.strftime('%m/%d/%Y %H:%M:%S')}'")

    def start_command(self) -> str:
        """启动命令。老代码：cd <workdir> && sudo nohup ./app_collect > /dev/null。"""
        return f'cd {self.workdir} && {self._sudo(f"nohup {self.program} > {self.log_file}")}'

    def stop_commands(self) -> List[str]:
        """停止命令。老代码停止时连杀两个进程：app_collect 和 gst-launch-1.0。"""
        return [self._sudo(f'pkill -f {pattern}') for pattern in self.stop_patterns]

    # ------------------------------------------------------------- 生命周期
    def connect(self) -> 'CollectCardLink':
        if not self.host:
            raise ValueError('links.ctrl 缺少 host（老 config.ini 的 [collect] ip）')
        self.state = LinkState.CONNECTING
        try:
            self.transport.connect(self.host, self.port, self.user, self.password, self.connect_timeout)
        except Exception as exc:
            self._fail(exc)
            raise
        self.state = LinkState.RUNNING
        return self

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:                               # noqa: BLE001
            log.debug('[ctrl] 关闭链路出错', exc_info=True)
        if self.state is not LinkState.ERROR:
            self.state = LinkState.STOPPED

    def start(self) -> CommandResult:
        """老 Collect_Thread.run()：连上 -> 对时 -> 起采集程序。"""
        self.connect()
        if self.sync_time_on_start:
            self.sync_time()
        return self.start_collect()

    run = start                                          # 老接口名，方便对照老代码

    def stop(self) -> List[CommandResult]:
        """老 Collect_Thread.stop()：先 pkill 掉远端采集程序，再关 SSH。"""
        try:
            results = self.stop_collect()
        except Exception as exc:
            self._fail(exc)
            raise
        finally:
            self.close()
        return results

    # ------------------------------------------------------------- 三条具体命令
    def sync_time(self, now: Optional[datetime] = None) -> CommandResult:
        return self._run_command(self.sync_command(now))

    def start_collect(self) -> CommandResult:
        result = self._run_command(self.start_command())
        if self.delay_after_start > 0:
            time.sleep(self.delay_after_start)           # 老代码起完程序 sleep 1 秒
        return result

    def stop_collect(self) -> List[CommandResult]:
        return [self._run_command(command) for command in self.stop_commands()]

    # ------------------------------------------------------------- 执行/状态
    def _run_command(self, command: str) -> CommandResult:
        started = time.time()
        try:
            result = self.transport.exec(command,
                                         password=self.password if self.use_sudo else None,
                                         timeout=self.read_timeout)
            if not isinstance(result, CommandResult):
                result = CommandResult(command=command, output=str(result))
            result.command = command
        except Exception as exc:                         # noqa: BLE001 - 单条命令失败不该炸整条链路
            result = CommandResult(command=command, error=f'{type(exc).__name__}: {exc}')
        result.duration = time.time() - started
        self.history.append(result)

        if result.ok:
            log.info('[ctrl] %s <- 退出码 %s', command, result.exit_status)
        else:
            self.last_error = result.error or f'退出码 {result.exit_status}'
            log.warning('[ctrl] %s <- %s', command, self.last_error)
        return result

    def _fail(self, exc: BaseException) -> None:
        self.state = LinkState.ERROR
        self.last_error = f'{type(exc).__name__}: {exc}'
        log.warning('[ctrl] 链路出错：%s', self.last_error)

    def status(self) -> Dict[str, Any]:
        last = self.history[-1] if self.history else None
        return {
            'name': self.name,
            'state': self.state.value,
            'target': self.target,
            'commands': len(self.history),
            'failed': sum(1 for item in self.history if not item.ok),
            'last_command': last.command if last else '',
            'last_error': self.last_error,
        }


# 已迁移的 A5 链路：配置里出现但这里没有的名字，会以「未迁移」跳过
LINK_REGISTRY: Dict[str, type] = {
    'ctrl': CollectCardLink,
}


def build_links(config: Optional[Dict[str, Any]] = None, transport: Any = None,
                names: Optional[List[str]] = None) -> List[Any]:
    """按配置建 A5 链路对象，只建 enabled 的。

    transport 只给测试注入用；真实运行留空，各链路自己建连接（ctrl 建 paramiko 客户端）。
    兼容老配置：ctrl 那段曾经放在 collectors 下面，这里也认。
    """
    section: Dict[str, Any] = {}
    if isinstance(config, dict):
        section = config.get('links') or {}
        if not section:
            legacy = (config.get('collectors') or {}).get('ctrl')
            if legacy:
                section = {'ctrl': legacy}

    links: List[Any] = []
    for name, cfg in section.items():
        if names and name not in names:
            continue
        if not cfg or not cfg.get('enabled'):
            continue
        klass = LINK_REGISTRY.get(name)
        if klass is None:
            log.warning('链路 %s 尚未迁移到新工程，已跳过（见 README 迁移清单）', name)
            continue
        links.append(klass(cfg, transport=transport) if transport is not None else klass(cfg))
    return links
