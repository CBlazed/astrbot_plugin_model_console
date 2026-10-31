"""SSH 隧道 + 远端 dashboard 端口发现（ssh 类型设备）。

行为对齐旧 Node 实现 ``model-console/src/ssh-tunnel.mjs``：

* 每次 ssh 调用都是**参数数组**且不经 shell（``asyncio.create_subprocess_exec``），host/用户名/路径
  不会被当作命令重新解释；
* 远端端口从远端日志最后一条 ``Starting WebUI at http://host:port`` 解析（``dataPath`` 可配），
  解析失败统一 ``DEVICE_PORT_UNKNOWN``；
* 隧道就绪探测失败/超时/ssh 提前退出 → ``DEVICE_SSH_FAILED``；本机没有 ssh → ``DEVICE_SSH_UNAVAILABLE``；
* 超时或异常都会清理子进程，``close()`` 幂等。

本模块不保存任何凭据：私钥留在磁盘上，也不打印配置。
"""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from .errors import ConsoleError

PORT_UNKNOWN = "DEVICE_PORT_UNKNOWN"
SSH_UNAVAILABLE = "DEVICE_SSH_UNAVAILABLE"
SSH_FAILED = "DEVICE_SSH_FAILED"

#: 远端日志里 dashboard 端口；同一日志多次出现时取最后一条。
START_PATTERN = re.compile(r"Starting WebUI at http://[^\s:]+:(\d+)")
_KNOWN_CODE = re.compile(r"^DEVICE_[A-Z_]+$")
_PROBE_TIMEOUT_SECONDS = 0.5

#: 注入点：接收 argv 数组，返回一个「进程」对象（``returncode`` / ``terminate`` / ``kill`` / ``wait``）。
Spawner = Callable[[list[str]], Awaitable[Any]]
Probe = Callable[[int], Awaitable[bool]]


def _fail(code: str) -> ConsoleError:
    """构造契约错误码异常（message 与 code 相同，与旧实现 ``Error(code)`` 对齐）。"""
    return ConsoleError(code, code)


def _known_code(value: Any) -> bool:
    """判断一个错误码是否符合 ``DEVICE_*`` 形式。"""
    return isinstance(value, str) and bool(_KNOWN_CODE.fullmatch(value))


def _normalize_failure(exc: BaseException) -> ConsoleError:
    """把非契约异常收敛成 ``DEVICE_SSH_FAILED``。"""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if isinstance(exc, ConsoleError):
        return exc
    if _known_code(code):
        return ConsoleError(code, str(code))
    if _known_code(message):
        return ConsoleError(str(message), str(message))
    return _fail(SSH_FAILED)


def _number_int(value: Any) -> int | None:
    """模拟 JS ``Number(x)`` + ``Number.isInteger``：返回整数或 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _connection_args(ssh: Any) -> tuple[list[str], str]:
    """构造 ssh 连接参数与 ``user@host`` 目标；配置不可用时抛 ``DEVICE_SSH_FAILED``。"""
    data = ssh if isinstance(ssh, dict) else {}
    host = data.get("host")
    user = data.get("user")
    if not isinstance(host, str) or not host or not isinstance(user, str) or not user:
        raise _fail(SSH_FAILED)
    port = 22 if data.get("port") is None else _number_int(data.get("port"))
    if port is None or port < 1 or port > 65535:
        raise _fail(SSH_FAILED)
    args = ["-p", str(port)]
    key_path = data.get("keyPath")
    if isinstance(key_path, str) and key_path != "":
        args.extend(["-i", key_path])
    return args, f"{user}@{host}"


def _remote_log_path(data_path: str) -> str:
    """把远端 data 目录转成 Windows 日志路径（统一反斜杠）。"""
    return str(data_path).rstrip("\\/") + "\\logs\\astrbot.log"


def _power_shell_literal(value: Any) -> str:
    """PowerShell 单引号字面量：内部单引号翻倍，绝不经 shell 转义。"""
    return "'" + str(value).replace("'", "''") + "'"


def _sync_free_port() -> int:
    """同步占一个 loopback 空闲端口并立刻释放。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def find_free_port() -> int:
    """向系统要一个当前空闲的 loopback 端口（返回前已释放监听）。"""
    return await asyncio.to_thread(_sync_free_port)


async def _default_run(argv: list[str]) -> str:
    """默认端口发现 runner：``ssh <argv>`` 一次，返回 stdout 文本。"""
    process = await asyncio.create_subprocess_exec(
        "ssh", *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=20.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    return stdout.decode("utf-8", errors="replace")


async def resolve_remote_port(ssh: Any, *, data_path: str | None = None,
                              run: Callable[[list[str]], Awaitable[Any]] | None = None) -> int:
    """从远端日志最后一条 ``Starting WebUI at`` 行解析 dashboard 端口。

    Args:
        ssh: ssh 配置块（``host``/``user`` 必填，``port``/``keyPath``/``dataPath`` 可选）。
        data_path: 覆盖 ``ssh.dataPath`` 的远端 AstrBot data 目录。
        run: 注入的 runner（接收 argv，返回 stdout 文本或 ``{"stdout": ...}``）。

    Returns:
        远端 dashboard 端口。

    Raises:
        ConsoleError: 任何失败都是 ``DEVICE_PORT_UNKNOWN``（不泄露底层细节）。
    """
    try:
        resolved_data = data_path
        if resolved_data is None and isinstance(ssh, dict):
            resolved_data = ssh.get("dataPath")
        if not isinstance(resolved_data, str) or resolved_data == "":
            raise _fail(PORT_UNKNOWN)
        connection, destination = _connection_args(ssh)
        command = (
            'powershell -NoProfile -NonInteractive -Command "Select-String -LiteralPath '
            + _power_shell_literal(_remote_log_path(resolved_data))
            + " -Pattern 'Starting WebUI at' -SimpleMatch | Select-Object -Last 1"
            + ' | ForEach-Object { $_.Line }"'
        )
        runner = run or _default_run
        output = await runner([*connection, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                               destination, command])
        if isinstance(output, str):
            text = output
        elif isinstance(output, dict):
            text = str(output.get("stdout") or "")
        else:
            text = str(getattr(output, "stdout", "") or "")
        matches = START_PATTERN.findall(text)
        if not matches:
            raise _fail(PORT_UNKNOWN)
        port = int(matches[-1])
        if port < 1 or port > 65535:
            raise _fail(PORT_UNKNOWN)
        return port
    except ConsoleError as error:
        if error.code == PORT_UNKNOWN:
            raise
        raise _fail(PORT_UNKNOWN) from None
    except Exception:  # noqa: BLE001 - 旧实现把所有失败都收敛成 PORT_UNKNOWN
        raise _fail(PORT_UNKNOWN) from None


async def _probe_port(port: int) -> bool:
    """做一次 TCP 连接探测；能连上即视为隧道就绪。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=_PROBE_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        return True
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass
        del reader


async def _spawn_ssh(argv: list[str]) -> Any:
    """默认 spawner：``ssh`` 直接用 argv 数组启动，stdio 全部丢弃。"""
    return await asyncio.create_subprocess_exec(
        "ssh", *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


class SshTunnel:
    """``ssh -N -L`` 本地端口转发隧道（async context manager）。

    用法::

        async with SshTunnel(ssh_config, base_url="http://127.0.0.1:6185") as base_url:
            adapter = HttpGatewayAdapter(base_url=base_url, api_key=key)

    进入时返回本地转发地址 ``http://127.0.0.1:<port>``；退出时幂等关闭 ssh 子进程。
    """

    def __init__(self, ssh: Any, *, timeout: float = 15.0,
                 remote_port: int | None = None, local_port: int | None = None,
                 remote_host: str = "127.0.0.1", base_url: str | None = None,
                 data_path: str | None = None,
                 spawn: Spawner | None = None, probe: Probe | None = None,
                 run: Callable[[list[str]], Awaitable[Any]] | None = None,
                 ready_timeout_ms: int | None = None,
                 retry_delay_ms: int = 100) -> None:
        self._ssh = ssh
        self._timeout = max(0.1, float(timeout))
        self._remote_port = remote_port
        self._local_port = local_port
        self._remote_host = remote_host
        self._base_url = base_url
        self._data_path = data_path
        self._spawn = spawn or _spawn_ssh
        self._probe = probe or _probe_port
        self._run = run
        self._ready_timeout = (ready_timeout_ms if ready_timeout_ms is not None
                               else int(self._timeout * 1000))
        self._retry_delay = max(0.0, retry_delay_ms / 1000.0)
        self._process: Any = None
        self._closed = False
        self.local_port: int | None = None
        self.base_url: str | None = None

    async def __aenter__(self) -> str:
        await self.start()
        assert self.base_url is not None
        return self.base_url

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _resolve_remote_port(self) -> int:
        """确定远端 dashboard 端口：显式参数 > 日志发现 > baseUrl 占位端口。"""
        if self._remote_port is not None:
            remote = _number_int(self._remote_port)
            if remote is None or remote < 1 or remote > 65535:
                raise _fail(SSH_FAILED)
            return remote
        data_path = self._data_path
        if data_path is None and isinstance(self._ssh, dict):
            data_path = self._ssh.get("dataPath")
        if isinstance(data_path, str) and data_path:
            return await resolve_remote_port(self._ssh, data_path=data_path, run=self._run)
        if self._base_url:
            try:
                remote = _number_int(urlsplit(self._base_url).port)
            except ValueError:
                raise _fail(SSH_FAILED) from None
            if remote is None or remote < 1 or remote > 65535:
                raise _fail(SSH_FAILED)
            return remote
        raise _fail(SSH_FAILED)

    async def start(self) -> str:
        """建立隧道并等到本地端口可连接；返回本地转发地址。"""
        if self._process is not None:
            return self.base_url or ""
        remote_port = await self._resolve_remote_port()
        connection, destination = _connection_args(self._ssh)
        local = self._local_port
        if local is None:
            local = await find_free_port()
        elif not isinstance(local, int) or local < 1 or local > 65535:
            raise _fail(SSH_FAILED)
        argv = [
            "-N", "-L", f"127.0.0.1:{local}:{self._remote_host}:{remote_port}",
            *connection,
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            destination,
        ]
        try:
            process = await self._spawn(argv)
        except FileNotFoundError:
            raise _fail(SSH_UNAVAILABLE) from None
        except OSError as error:
            code = getattr(error, "code", None) or getattr(error, "errno", None)
            if code in ("ENOENT", 2, None) and isinstance(error, FileNotFoundError):
                raise _fail(SSH_UNAVAILABLE) from None
            if str(code) == "ENOENT":
                raise _fail(SSH_UNAVAILABLE) from None
            raise _fail(SSH_FAILED) from None
        except ConsoleError:
            raise
        except Exception:  # noqa: BLE001 - spawn 失败统一映射
            raise _fail(SSH_FAILED) from None
        self._process = process
        try:
            await self._wait_ready(local)
        except BaseException:
            await self.close()
            raise
        self.local_port = local
        self.base_url = f"http://127.0.0.1:{local}"
        return self.base_url

    async def _wait_ready(self, port: int) -> None:
        """轮询本地端口直到可连接、进程提前退出或超时。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout / 1000.0
        while True:
            if self._process is not None and getattr(self._process, "returncode", None) is not None:
                raise _fail(SSH_FAILED)
            if await self._probe(port):
                return
            if self._process is not None and getattr(self._process, "returncode", None) is not None:
                raise _fail(SSH_FAILED)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _fail(SSH_FAILED)
            await asyncio.sleep(min(self._retry_delay, remaining))

    async def close(self) -> None:
        """幂等关闭：SIGTERM → 1 秒后 SIGKILL，并回收子进程。"""
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if getattr(process, "returncode", None) is None:
                try:
                    process.terminate()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
        except Exception:  # noqa: BLE001 - 清理失败不影响调用方
            pass
        finally:
            self._process = None
