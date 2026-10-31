"""本机 Model Console Agent（仅监听 ``127.0.0.1``）。

对齐旧 Node 实现 ``model-console/src/agent.mjs``：

* Hub 用 8 位一次性配对码（10 分钟有效、一次即焚）换 64 hex 设备 token；
* ``<state>/agent.json`` 只保存 sha256 哈希，绝不保存配对码或 token 明文；
* 默认只读：``/agent/v1/plans``、``/apply``、``/backups`` 一律 403 ``AGENT_READ_ONLY``；
  只有启动时带 ``--enable-writes`` 才允许写入；
* 公开端点只有 ``/agent/v1/health``，其余都要 ``Authorization: Bearer <token>``；
* ``/agent/v1/apply`` 附带基于本机日志尾部的热重载证据 ``hotLoad``（读不到就如实报告
  ``checked: true, verified: false`` 的 note），绝不把「没证据」说成成功。

独立运行（等价旧 ``node src/agent.mjs``）::

    python -m console.agent [--port=0] [--state=<dir>] [--data=<dir>] [--enable-writes]

启动后向 stdout 打印 ``{"url": "...", "pairingCode": "12345678", "expiresAt": "..."}``。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from .errors import ConsoleError, known_error

AGENT_STATE_FILE = "agent.json"
PAIRING_TTL_MS = 10 * 60 * 1000
PAIRING_CODE_PATTERN = re.compile(r"^\d{8}$")
TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$", re.IGNORECASE)
PAIR_FAIL_LIMIT = 5
PAIR_FAIL_WINDOW_MS = 60 * 1000
MAX_BODY_BYTES = 16 * 1024
HOT_LOAD_TAIL_LINES = 200
HOT_LOAD_TAIL_BYTES = 128 * 1024
HOT_LOAD_PATTERN = re.compile(r"reload|hot[ -]?reload|热重载|配置已更新", re.IGNORECASE)
AGENT_READ_ONLY = "AGENT_READ_ONLY"

_PLAN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SHORT_BACKUP_ID = re.compile(r"^[0-9a-f]{8,12}$", re.I)
_FULL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SQLITE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")


class _BodyError(Exception):
    """请求体相关的稳定错误码（内部使用）。"""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _now_iso(now_ms: float | None = None) -> str:
    """UTC ISO 时间（毫秒 + Z），与 JS ``toISOString`` 一致。"""
    moment = (time.time() * 1000) if now_ms is None else float(now_ms)
    stamp = datetime.fromtimestamp(moment / 1000.0, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _sha256_hex(value: str) -> str:
    """UTF-8 字符串的 sha256 hex。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_equal_hex(left: Any, right: Any) -> bool:
    """常量时间比较两个 64-hex 摘要；格式非法直接 False。"""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not TOKEN_PATTERN.match(left) or not TOKEN_PATTERN.match(right):
        return False
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def _bearer_token(headers: Any) -> str | None:
    """从 Authorization 头取出唯一的 64-hex Bearer token。"""
    header = headers.get("Authorization") if headers is not None else None
    if not isinstance(header, str):
        return None
    match = re.fullmatch(r"Bearer ([a-f0-9]{64})", header, re.IGNORECASE)
    return match.group(1).lower() if match else None


def sanitize_error_details(details: Any) -> dict[str, Any]:
    """白名单化错误详情（id/backupId/outcome/code，绝不带路径）。"""
    safe: dict[str, Any] = {}
    if not isinstance(details, dict):
        return safe

    def usable(value: Any) -> bool:
        return isinstance(value, str) and 0 < len(value) <= 128

    if usable(details.get("id")) and _PLAN_ID.match(details["id"]):
        safe["id"] = details["id"]
    if usable(details.get("backupId")) and (
            _SHORT_BACKUP_ID.match(details["backupId"])
            or _FULL_UUID.match(details["backupId"])):
        safe["backupId"] = details["backupId"]
    if usable(details.get("outcome")) and _SAFE_TOKEN.match(details["outcome"]):
        safe["outcome"] = details["outcome"]
    if usable(details.get("code")) and _SAFE_CODE.match(details["code"]):
        safe["code"] = details["code"]
    return safe


def public_backup(item: Any) -> dict[str, Any] | None:
    """备份条目的契约字段白名单（id 必须是 UUID/short，绝不输出绝对路径）。"""
    if not isinstance(item, dict):
        return None
    identity = item.get("id")
    if not isinstance(identity, str) or not (_FULL_UUID.match(identity)
                                            or _SHORT_BACKUP_ID.match(identity)):
        return None
    entry: dict[str, Any] = {"id": identity}
    created_at = item.get("createdAt")
    if (isinstance(created_at, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", created_at)
            and len(created_at) <= 40):
        entry["createdAt"] = created_at
    byte_count = item.get("bytes")
    if isinstance(byte_count, (int, float)) and not isinstance(byte_count, bool):
        if byte_count >= 0 and byte_count == byte_count and abs(byte_count) != float("inf"):
            entry["bytes"] = int(byte_count)
    files = item.get("files")
    if isinstance(files, int) and not isinstance(files, bool) and files >= 0:
        entry["files"] = files
    sqlite = item.get("sqlite")
    if isinstance(sqlite, str) and _SQLITE_NAME.match(sqlite):
        entry["sqlite"] = sqlite
    return entry


def _read_log_tail(path: Path | None, *, max_lines: int = HOT_LOAD_TAIL_LINES,
                   max_bytes: int = HOT_LOAD_TAIL_BYTES) -> list[str] | None:
    """同步读取日志尾部（最多 max_bytes、最后 max_lines 行）；不可读返回 None。"""
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not os.path.isfile(path):
                return None
            size = info.st_size
            if size == 0:
                return []
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read(size - start)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    lines = [line.rstrip("\r") for line in text.split("\n")]
    return [line for line in lines[-max_lines:] if line]


async def hot_load_evidence(data_dir: Path | None) -> dict[str, Any]:
    """基于本机日志尾部判断热重载是否发生；永不抛异常。"""
    log_path = Path(data_dir) / "logs" / "astrbot.log" if data_dir is not None else None
    lines = await asyncio.to_thread(_read_log_tail, log_path)
    if lines is None:
        return {"verified": False, "checked": True,
                "note": "无法读取本机日志，未确认热重载是否发生"}
    marker = next((line for line in lines if HOT_LOAD_PATTERN.search(line)), None)
    if marker is not None:
        return {"verified": True, "checked": True, "marker": marker.strip()[:200]}
    return {"verified": False, "checked": True,
            "note": f"日志尾部 {min(len(lines), HOT_LOAD_TAIL_LINES)} 行未发现热重载标记，"
                    f"配置可能需重启生效"}


async def _load_token_hash(state_path: Path) -> str | None:
    """读取已持久化的 token 哈希；任何损坏都视为「尚未配对」。"""
    try:
        raw = await asyncio.to_thread(state_path.read_text, encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw.lstrip("\ufeff"))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    token_hash = parsed.get("tokenHash")
    if isinstance(token_hash, str) and TOKEN_PATTERN.match(token_hash):
        return token_hash.lower()
    return None


def _save_state_sync(state_path: Path, token_hash: str | None,
                     pairing_hash: str | None, pairing_expires_at: str | None,
                     now_ms: float) -> None:
    """同步原子写 agent 状态（只含哈希）。"""
    state = {
        "version": 1,
        "tokenHash": token_hash,
        "pairing": ({"hash": pairing_hash, "expiresAt": pairing_expires_at}
                    if pairing_hash else None),
        "updatedAt": _now_iso(now_ms),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = f"{state_path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(state, ensure_ascii=False) + "\n")
            handle.flush()
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
        os.replace(temporary, state_path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


async def _save_state(state_path: Path, token_hash: str | None,
                      pairing_hash: str | None, pairing_expires_at: str | None,
                      now_ms: float) -> None:
    """异步保存 agent 状态。"""
    await asyncio.to_thread(_save_state_sync, state_path, token_hash, pairing_hash,
                            pairing_expires_at, now_ms)


async def _backup_stats(engine: Any) -> dict[str, Any]:
    """按 Engine 能力计算备份统计（无 ``backup_stats`` 时由列表汇总）。"""
    stats_method = getattr(engine, "backup_stats", None)
    if callable(stats_method):
        stats = await stats_method()
        return stats if isinstance(stats, dict) else {}
    lister = getattr(engine, "list_backups", None)
    items = await lister() if callable(lister) else []
    items = items if isinstance(items, list) else []
    total = 0
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("bytes"), (int, float)):
            total += max(0, int(item["bytes"]))
    return {"count": len(items), "bytes": total}


def _backup_limits() -> dict[str, int]:
    """备份保留上限（环境变量与旧实现一致）。"""
    keep_raw = os.environ.get("MODEL_CONSOLE_BACKUP_KEEP")
    keep = 10
    if keep_raw is not None:
        try:
            value = int(str(keep_raw).strip())
            if 1 <= value <= 100:
                keep = value
        except ValueError:
            pass
    size_raw = os.environ.get("MODEL_CONSOLE_BACKUP_MAX_MB")
    max_bytes = 2048 * 1024 * 1024
    if size_raw is not None:
        try:
            value = float(str(size_raw).strip())
            if value >= 0 and value == value:
                max_bytes = int(value * 1024 * 1024)
        except ValueError:
            pass
    return {"keep": keep, "maxBytes": max_bytes}


def _plugin_version() -> str | None:
    """从插件 ``metadata.yaml`` 读版本号；读不到返回 None（不影响启动）。"""
    try:
        metadata = Path(__file__).resolve().parent.parent / "metadata.yaml"
        for line in metadata.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^version:\s*(\S+)\s*$", line)
            if match:
                return match.group(1).strip("'\"")
    except (OSError, UnicodeError):
        return None
    return None


_VERSION = _plugin_version()


@web.middleware
async def _agent_middleware(request: web.Request,
                            handler: Any) -> web.StreamResponse:
    """统一响应头与异常收敛：未捕获异常 → 500 ``AGENT_INTERNAL``。"""
    try:
        response = await handler(request)
    except _BodyError as error:
        response = web.json_response({"error": error.code}, status=error.status)
    except web.HTTPRequestEntityTooLarge:
        response = web.json_response({"error": "AGENT_BODY_TOO_LARGE"}, status=413)
    except web.HTTPException as error:
        response = web.json_response({"error": "AGENT_NOT_FOUND"}, status=error.status)
    except Exception:  # noqa: BLE001 - 不能让栈泄露给 Hub
        response = web.json_response({"error": "AGENT_INTERNAL"}, status=500)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class AgentHandle:
    """运行中的 agent 句柄（``close()`` 幂等）。"""

    def __init__(self, *, url: str, port: int, state_path: Path, pairing_code: str,
                 expires_at: str, token_hash: str | None, runner: web.AppRunner,
                 gateway: Any = None) -> None:
        self.url = url
        self.port = port
        self.state_path = state_path
        self.pairing_code = pairing_code
        self.expires_at = expires_at
        self.token_hash = token_hash
        self._runner = runner
        self._gateway = gateway
        self._closed = False

    async def close(self) -> None:
        """停止 HTTP 服务并释放资源。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._runner.cleanup()
        except Exception:  # noqa: BLE001
            pass
        closer = getattr(self._gateway, "close", None)
        if callable(closer):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


async def start_agent(*, engine: Any = None, data_dir: Path | None = None,
                      state_dir: Path, port: int = 0, write_enabled: bool = False,
                      now: Any = None, pairing_ttl_ms: int = PAIRING_TTL_MS,
                      pair_fail_limit: int = PAIR_FAIL_LIMIT,
                      pair_fail_window_ms: int = PAIR_FAIL_WINDOW_MS) -> AgentHandle:
    """启动仅监听 127.0.0.1 的 agent HTTP 服务。

    Args:
        engine: 引擎对象（``snapshot/plan/apply/list_backups``）；None 时用 Gateway+Engine 构建。
        data_dir: 本机 AstrBot data 目录（用于热重载日志证据）。
        state_dir: agent 状态目录（保存 ``agent.json`` 哈希）。
        port: 监听端口，0 = 系统分配。
        write_enabled: 是否允许远程写（默认只读）。
        now: 时钟注入（毫秒），测试用。
        pairing_ttl_ms: 配对码有效期。
        pair_fail_limit: 窗口内允许的配对失败次数。
        pair_fail_window_ms: 失败计数窗口。

    Returns:
        :class:`AgentHandle`（含 url/pairing_code/expires_at/close()）。
    """
    clock = now or (lambda: time.time() * 1000)
    state_path = Path(state_dir) / AGENT_STATE_FILE
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    token_hash = await _load_token_hash(state_path)
    pairing_code = f"{_secrets.randbelow(100000000):08d}"
    pairing_hash = _sha256_hex(pairing_code)
    started_ms = clock()
    expires_ms = started_ms + pairing_ttl_ms
    expires_at = _now_iso(expires_ms)
    await _save_state(state_path, token_hash, pairing_hash, expires_at, started_ms)

    gateway = None
    if engine is None:
        resolved_data = data_dir
        if resolved_data is None:
            raise ConsoleError("ASTRBOT_DATA_REQUIRED",
                               "缺少 AstrBot data 目录，无法构建本机引擎")
        from .engine import Engine
        from .gateway import Gateway

        gateway = Gateway({}, Path(resolved_data))
        engine = Engine(gateway, Path(state_dir), Path(resolved_data),
                        _backup_limits(), not write_enabled)

    # 可变状态集中在一个 dict 里，闭包直接读写，避免 nonlocal 与「先读后声明」问题。
    auth_state: dict[str, Any] = {
        "tokenHash": token_hash,
        "pairingHash": pairing_hash,
        "pairingExpiresMs": expires_ms,
    }
    failure_times: list[float] = []

    def failures_in_window() -> int:
        cutoff = clock() - pair_fail_window_ms
        while failure_times and failure_times[0] <= cutoff:
            failure_times.pop(0)
        return len(failure_times)

    async def read_json_body(request: web.Request) -> Any:
        raw = await request.read()
        if len(raw) > MAX_BODY_BYTES:
            raise _BodyError("AGENT_BODY_TOO_LARGE", 413)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _BodyError("AGENT_INVALID_JSON", 400) from None

    def json_required(request: web.Request) -> Any:
        content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise _BodyError("AGENT_JSON_REQUIRED", 415)
        return None

    async def handle(request: web.Request) -> web.Response:
        path = request.path
        method = request.method
        if path == "/agent/v1/health" and method == "GET":
            return web.json_response({"status": "ok", "version": _VERSION})
        if path == "/agent/v1/pair" and method == "POST":
            json_required(request)
            if failures_in_window() >= pair_fail_limit:
                return web.json_response({"error": "AGENT_RATE_LIMITED"}, status=429)
            body = await read_json_body(request)
            if (not isinstance(body, dict) or not isinstance(body.get("code"), str)):
                return web.json_response({"error": "AGENT_INVALID_REQUEST"}, status=400)
            submitted = body["code"]
            matches = bool(PAIRING_CODE_PATTERN.match(submitted)
                           and auth_state["pairingHash"]
                           and _safe_equal_hex(_sha256_hex(submitted),
                                               auth_state["pairingHash"]))
            expired = clock() >= auth_state["pairingExpiresMs"]
            if not matches or expired or not auth_state["pairingHash"]:
                failure_times.append(clock())
                return web.json_response({"error": "AGENT_UNAUTHORIZED"}, status=401)
            token_value = _secrets.token_hex(32)
            auth_state["tokenHash"] = _sha256_hex(token_value)
            auth_state["pairingHash"] = None
            auth_state["pairingExpiresMs"] = 0
            failure_times.clear()
            try:
                await _save_state(state_path, auth_state["tokenHash"], None, None, clock())
            except OSError:
                return web.json_response({"error": "AGENT_STATE_WRITE_FAILED"}, status=500)
            return web.json_response({"token": token_value})

        token = _bearer_token(request.headers)

        def authenticated() -> bool:
            if not token or not auth_state["tokenHash"]:
                return False
            return _safe_equal_hex(_sha256_hex(token), auth_state["tokenHash"])

        async def json_body() -> tuple[Any, bool]:
            json_required(request)
            body = await read_json_body(request)
            return body, True

        if path == "/agent/v1/snapshot" and method == "GET":
            if not authenticated():
                return web.json_response({"error": "AGENT_UNAUTHORIZED"}, status=401)
            try:
                snapshot = await engine.snapshot()
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "AGENT_SNAPSHOT_FAILED"}, status=500)
            return web.json_response(snapshot)
        if path == "/agent/v1/plans" and method == "POST":
            if not authenticated():
                return web.json_response({"error": "AGENT_UNAUTHORIZED"}, status=401)
            if not write_enabled:
                return web.json_response({"error": AGENT_READ_ONLY}, status=403)
            body, _ok = await json_body()
            if not isinstance(body, dict):
                return web.json_response({"error": "AGENT_INVALID_REQUEST"}, status=400)
            try:
                plan = await engine.plan(body.get("resource"), body.get("changes"),
                                         False, body.get("expectedRevision"))
            except Exception as error:  # noqa: BLE001
                return web.json_response(
                    {"error": known_error(error), **sanitize_error_details(getattr(error, "details", None))},
                    status=409)
            return web.json_response(plan)
        if path == "/agent/v1/apply" and method == "POST":
            if not authenticated():
                return web.json_response({"error": "AGENT_UNAUTHORIZED"}, status=401)
            if not write_enabled:
                return web.json_response({"error": AGENT_READ_ONLY}, status=403)
            body, _ok = await json_body()
            if not isinstance(body, dict):
                return web.json_response({"error": "AGENT_INVALID_REQUEST"}, status=400)
            try:
                result = await engine.apply(body.get("id"))
            except Exception as error:  # noqa: BLE001
                return web.json_response(
                    {"error": known_error(error), **sanitize_error_details(getattr(error, "details", None))},
                    status=409)
            evidence = await hot_load_evidence(data_dir)
            return web.json_response({"result": result, "hotLoad": evidence})
        if path == "/agent/v1/backups" and method == "GET":
            if not authenticated():
                return web.json_response({"error": "AGENT_UNAUTHORIZED"}, status=401)
            if not write_enabled:
                return web.json_response({"error": AGENT_READ_ONLY}, status=403)
            try:
                backups = await engine.list_backups()
                stats = await _backup_stats(engine)
            except Exception as error:  # noqa: BLE001
                return web.json_response(
                    {"error": known_error(error), **sanitize_error_details(getattr(error, "details", None))},
                    status=409)
            items = backups if isinstance(backups, list) else []
            count = stats.get("count")
            byte_count = stats.get("bytes")
            return web.json_response({
                "backups": [entry for entry in (public_backup(item) for item in items)
                            if entry is not None],
                "stats": {
                    "count": count if isinstance(count, int) and not isinstance(count, bool)
                    else len(items),
                    "bytes": (max(0, int(byte_count))
                              if isinstance(byte_count, (int, float))
                              and not isinstance(byte_count, bool) else 0),
                },
                "limits": _backup_limits(),
            })
        return web.json_response({"error": "AGENT_NOT_FOUND"}, status=404)

    application = web.Application(middlewares=[_agent_middleware],
                                  client_max_size=MAX_BODY_BYTES)
    application.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", int(port))
    await site.start()
    actual_port = int(port)
    addresses = getattr(runner, "addresses", None)
    if addresses:
        try:
            actual_port = int(addresses[0][1])
        except (IndexError, TypeError, ValueError):
            actual_port = int(port)
    return AgentHandle(url=f"http://127.0.0.1:{actual_port}", port=actual_port,
                       state_path=state_path, pairing_code=pairing_code,
                       expires_at=expires_at, token_hash=token_hash, runner=runner,
                       gateway=gateway)


# --------------------------------------------------------------------- CLI


def _option_value(argv: list[str], name: str) -> str | None:
    """解析 ``--name=value`` 或 ``--name value``；不存在返回 None。"""
    prefix = f"--{name}="
    for argument in argv:
        if argument.startswith(prefix):
            return argument[len(prefix):]
    if f"--{name}" in argv:
        index = argv.index(f"--{name}")
        if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            return argv[index + 1]
    return None


async def _run_cli() -> int:
    """CLI 主流程：解析参数、构建引擎、打印配对信息、等待退出信号。"""
    argv = sys.argv[1:]
    port_raw = _option_value(argv, "port")
    if port_raw is None:
        port = 0
    else:
        try:
            port = int(port_raw)
        except ValueError:
            port = -1
    if port < 0 or port > 65535:
        print("AGENT_INVALID_PORT: --port 必须是 0-65535 的整数。", file=sys.stderr)
        return 1
    write_enabled = "--enable-writes" in argv
    state_override = _option_value(argv, "state")
    data_override = _option_value(argv, "data")
    try:
        from .paths import discover_astrbot_data, resolve_state_dir
    except Exception:  # noqa: BLE001 - paths 尚未实现时给出可读错误
        print("Agent startup failed (PATHS_UNAVAILABLE): console.paths 不可用。",
              file=sys.stderr)
        return 1
    state_dir = (Path(state_override).resolve() if state_override
                 else Path(resolve_state_dir({})))
    try:
        data_dir = (Path(data_override).resolve() if data_override
                    else Path(await discover_astrbot_data({})))
    except ConsoleError as error:
        if error.code == "ASTRBOT_DATA_REQUIRED":
            print(error.message or "ASTRBOT_DATA_REQUIRED", file=sys.stderr)
            print("请先启动一次 AstrBot，或用 --data / 环境变量 ASTRBOT_DATA 指定其 data 目录后重试。",
                  file=sys.stderr)
            return 1
        raise
    try:
        handle = await start_agent(data_dir=data_dir, state_dir=state_dir,
                                   port=port, write_enabled=write_enabled)
    except ConsoleError as error:
        print(f"Agent startup failed ({error.code}): {error.message}", file=sys.stderr)
        return 1
    print(json.dumps({"url": handle.url, "pairingCode": handle.pairing_code,
                      "expiresAt": handle.expires_at}, ensure_ascii=False), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(stop.set))
            except (ValueError, OSError):
                pass
    try:
        await stop.wait()
    finally:
        await handle.close()
    return 0


def main() -> int:
    """``python -m console.agent`` 入口。"""
    try:
        return asyncio.run(_run_cli())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover - 独立运行入口
    raise SystemExit(main())
