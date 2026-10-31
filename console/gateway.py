"""本机 AstrBot Dashboard API 适配层（旧实现 ``gateway.mjs`` 的 Python 移植）。

职责：
* 复用单个 :class:`httpx.AsyncClient`（15s 超时），自签 HS256 JWT 调本机 Dashboard；
* 解析 Dashboard 端口（环境变量 → ``cmd_config.json`` → 日志末条 ``Starting WebUI at``）；
* 把 Dashboard 的 ``{status:"ok",data:...}`` 收敛成资源对象，并映射成稳定错误码。

凭据与上游响应体**不离开本模块**：错误只抛稳定错误码（``DASHBOARD_NOT_FOUND`` /
``ASTRBOT_UNREACHABLE`` / ``ASTRBOT_HTTP_<status>`` / ``ASTRBOT_INVALID_RESPONSE`` /
``ASTRBOT_API_REJECTED`` / ``INVALID_RESOURCE`` / ``INVALID_API_PATH``）。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx

from . import paths
from .errors import ConsoleError

#: Dashboard 日志里「WebUI 已启动」的端口提示。
_PORT_LINE = re.compile(rb"Starting WebUI at http://[^\s:]+:(\d+)")
#: 只钉平台实例、不钉会话的路由形态。
_PLATFORM_LEVEL_ROUTE = re.compile(r"^[^:]+::$")
#: 只允许调 AstrBot 官方 v1 API。
_API_PREFIX = "/api/v1/"
#: 本地模型服务（仅这类地址才允许探测推理档位）。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
#: API 调用超时（秒）。
DEFAULT_TIMEOUT = 15.0
#: 推理档位探测超时（秒，旧实现 5000）。
EFFORTS_TIMEOUT = 5.0
#: 端口探测时每次向前读的字节数。
_LOG_CHUNK = 65536


def _b64url(raw: bytes) -> str:
    """base64url 编码（无填充，等价 Node ``toString('base64url')``）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _parse_port(value: Any) -> int | None:
    """把候选端口值收敛成正整数端口；非法返回 ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and 1 <= value <= 65535:
        return value
    return None


def _quote(value: Any) -> str:
    """URL 查询参数编码（等价 ``encodeURIComponent``）。"""
    return quote(str(value), safe="")


def _read_json_file(path: Path) -> Any:
    """读 UTF-8（允许 BOM）JSON；文件缺失或非法返回 ``None``。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _scan_log_port(path: Path, size: int) -> int | None:
    """从日志末尾向前按 64 KiB 块扫描最后一条 ``Starting WebUI at`` 端口。"""
    carry = b""
    end = size
    try:
        with open(path, "rb") as handle:
            while end > 0:
                start = max(0, end - _LOG_CHUNK)
                handle.seek(start)
                chunk = handle.read(end - start)
                text = chunk + carry
                matches = _PORT_LINE.findall(text)
                if matches:
                    return int(matches[-1])
                carry = text[:256]
                end = start
    except OSError:
        return None
    return None


def _list_plugin_config_files(config_dir: Path) -> list[str]:
    """列出 ``data/config`` 下以 ``_config.json`` 结尾的文件名。"""
    try:
        entries = list(os.scandir(config_dir))
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if entry.name.endswith("_config.json") and entry.is_file()
    )


def profile_identity(
    profile: Mapping[str, Any],
    platforms: list[Mapping[str, Any]] | None = None,
    routing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """由 AstrBot 平台实例与路由推导配置档案身份（旧 ``identity.mjs`` 的等价物）。

    身份只来自平台实例与显式路由，绝不来自人格名或文件名。

    Args:
        profile: 档案摘要（含 ``id`` 或 ``uuid``）。
        platforms: 平台实例列表 ``[{id,type,status}]``。
        routing: 路由表 ``{"<platform_id>::": "<profile_id>"}``。

    Returns:
        身份视图 ``{name,transport,association,instances,account,nickname,routingStatus}``。
    """
    platform_list = list(platforms or [])
    route_map = dict(routing or {})
    identifier = profile.get("id", profile.get("uuid"))
    identifier = identifier if isinstance(identifier, str) else ""
    linked = [
        platform
        for platform in platform_list
        if any(
            target == identifier and pattern == f"{platform.get('id')}::"
            for pattern, target in route_map.items()
        )
    ]

    def category_of(platform: Mapping[str, Any]) -> str:
        """平台类型 → 中文类别。"""
        platform_type = platform.get("type")
        if platform_type == "qq_official":
            return "QQ机器人"
        if platform_type == "aiocqhttp":
            return "QQ机器人（OneBot）"
        return str(platform_type)

    if identifier == "default":
        source_platforms = [
            platform
            for platform in platform_list
            if not any(pattern == f"{platform.get('id')}::" for pattern in route_map)
        ]
    else:
        source_platforms = linked
    instances = [
        {
            "instanceId": platform.get("id"),
            "type": platform.get("type"),
            "category": category_of(platform),
            "status": platform.get("status") or "unknown",
            "source": "AstrBot实例接口",
        }
        for platform in source_platforms
    ]
    categories: list[str] = []
    for instance in instances:
        category = instance["category"]
        if category not in categories:
            categories.append(category)
    joined = " / ".join(categories)
    if identifier == "default":
        name = "全局默认配置"
    elif linked:
        name = joined
    else:
        name = f"未关联配置 · {identifier[:8]}"
    return {
        "name": name,
        "transport": "全局默认" if identifier == "default" else (joined if linked else "配置档案（非机器人身份）"),
        "association": "default" if identifier == "default" else ("platform-route" if linked else "unassigned"),
        "instances": instances,
        "account": None,
        "nickname": None,
        "routingStatus": "未命中专用路由的会话使用此配置"
        if identifier == "default"
        else ("已关联平台级路由" if linked else "未发现平台级路由；不作为已接入机器人展示"),
    }


class Gateway:
    """本机 AstrBot Dashboard API 客户端（进程内单例式复用）。"""

    def __init__(
        self,
        config: Mapping[str, Any],
        data_dir: Path | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """创建网关。

        Args:
            config: 插件配置（含 ``astrbot_data`` 覆盖项）。
            data_dir: AstrBot data 目录；``None`` 时在首次调用时发现。
            transport: 仅供测试注入 ``httpx.MockTransport``。
            timeout: API 调用超时（秒）。
        """
        self.config: dict[str, Any] = dict(config or {})
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._read_only = False
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        )
        self._log_cache: dict[str, Any] | None = None
        self._config_cache: dict[str, Any] | None = None
        self._log_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        #: 实际发生的文件读取次数（日志/配置），便于诊断，单调递增。
        self.reads: dict[str, int] = {"log": 0, "config": 0}

    # ------------------------------------------------------------------ 生命周期
    def set_read_only(self, value: bool) -> None:
        """设置只读标记；只读时 :meth:`write` 直接拒绝。"""
        self._read_only = bool(value)

    def invalidate(self) -> None:
        """丢弃进程内文件缓存（端口与 ``cmd_config.json``），下次调用重新读取。"""
        self._log_cache = None
        self._config_cache = None

    async def close(self) -> None:
        """释放底层 HTTP 客户端。"""
        await self._client.aclose()

    @property
    def data_dir(self) -> Path | None:
        """已解析的 AstrBot data 目录（未解析时为 ``None``）。"""
        return self._data_dir

    async def _data_dir_path(self) -> Path:
        """惰性解析 AstrBot data 目录。"""
        if self._data_dir is None:
            self._data_dir = await asyncio.to_thread(
                paths.discover_astrbot_data,
                self.config,
            )
        return self._data_dir

    # ------------------------------------------------------------------ 端口与签名
    async def _dashboard_config(self) -> dict[str, Any]:
        """读取 ``cmd_config.json``（按 size+mtime 缓存）。"""
        async with self._config_lock:
            data_dir = await self._data_dir_path()
            path = data_dir / "cmd_config.json"
            try:
                info = await asyncio.to_thread(path.stat)
            except OSError:
                return {}
            cached = self._config_cache
            if (
                cached
                and cached["size"] == info.st_size
                and cached["mtime"] == info.st_mtime_ns
            ):
                return cached["config"]
            raw = await asyncio.to_thread(_read_json_file, path)
            config = raw if isinstance(raw, dict) else {}
            self.reads["config"] += 1
            self._config_cache = {
                "size": info.st_size,
                "mtime": info.st_mtime_ns,
                "config": config,
            }
            return config

    async def _log_port(self) -> int | None:
        """从日志末条 ``Starting WebUI at`` 取端口（按 size+mtime 缓存）。"""
        async with self._log_lock:
            data_dir = await self._data_dir_path()
            path = data_dir / "logs" / "astrbot.log"
            try:
                info = await asyncio.to_thread(path.stat)
            except OSError:
                return None
            cached = self._log_cache
            if (
                cached
                and cached["size"] == info.st_size
                and cached["mtime"] == info.st_mtime_ns
            ):
                return cached["port"]
            port = await asyncio.to_thread(_scan_log_port, path, info.st_size)
            self.reads["log"] += 1
            self._log_cache = {
                "size": info.st_size,
                "mtime": info.st_mtime_ns,
                "port": port,
            }
            return port

    async def _port(self) -> int | None:
        """按契约顺序解析 Dashboard 端口。"""
        for name in ("DASHBOARD_PORT", "ASTRBOT_DASHBOARD_PORT"):
            port = _parse_port(os.environ.get(name))
            if port:
                return port
        config = await self._dashboard_config()
        dashboard = config.get("dashboard")
        if isinstance(dashboard, Mapping):
            port = _parse_port(dashboard.get("port"))
            if port:
                return port
        return await self._log_port()

    @staticmethod
    def _sign(secret: str, username: str) -> str:
        """生成 HS256 JWT（claim 只有 ``username``）。"""
        header = _b64url(
            json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
        )
        body = _b64url(
            json.dumps({"username": username}, separators=(",", ":")).encode("utf-8")
        )
        signing_input = f"{header}.{body}".encode("ascii")
        signature = _b64url(
            hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        )
        return f"{header}.{body}.{signature}"

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        """调用本机 Dashboard API 并返回 ``data``。

        Args:
            method: HTTP 方法（``GET`` / ``PUT`` / ``POST``）。
            path: 必须以 ``/api/v1/`` 开头。
            body: 可选 JSON 请求体。

        Returns:
            Dashboard 响应里的 ``data`` 字段。

        Raises:
            ConsoleError: 稳定错误码（见模块 docstring）。
        """
        if not isinstance(path, str) or not path.startswith(_API_PREFIX):
            raise ConsoleError("INVALID_API_PATH")
        port = await self._port()
        if not port:
            raise ConsoleError("DASHBOARD_NOT_FOUND")
        config = await self._dashboard_config()
        dashboard = config.get("dashboard")
        dashboard = dashboard if isinstance(dashboard, Mapping) else {}
        secret = dashboard.get("jwt_secret")
        secret = secret if isinstance(secret, str) else ""
        username = dashboard.get("username")
        username = username.strip() if isinstance(username, str) and username.strip() else "astrbot"
        token = self._sign(secret, username)
        content = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.request(
                method.upper(),
                f"http://127.0.0.1:{port}{path}",
                content=content,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ConsoleError("ASTRBOT_UNREACHABLE") from exc
        if response.is_redirect:
            raise ConsoleError("ASTRBOT_UNREACHABLE")
        if response.status_code >= 400:
            raise ConsoleError(f"ASTRBOT_HTTP_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConsoleError("ASTRBOT_INVALID_RESPONSE") from exc
        if not isinstance(payload, Mapping) or payload.get("status") != "ok":
            raise ConsoleError("ASTRBOT_API_REJECTED")
        return payload.get("data")

    # ------------------------------------------------------------------ 读写资源
    async def read(self, resource: Mapping[str, Any]) -> Any:
        """读取一个资源（provider / profile / plugin）的当前配置对象。"""
        kind = resource.get("kind")
        identifier = _quote(resource.get("id"))
        if kind == "provider":
            data = await self.call(
                "GET", f"/api/v1/providers/by-id?provider_id={identifier}"
            )
            return data.get("provider") if isinstance(data, Mapping) else None
        if kind == "profile":
            data = await self.call("GET", f"/api/v1/config-profiles/{identifier}")
            return data.get("config") if isinstance(data, Mapping) else None
        if kind == "plugin":
            data = await self.call(
                "GET", f"/api/v1/plugins/config?plugin_id={identifier}"
            )
            return data.get("config") if isinstance(data, Mapping) else None
        raise ConsoleError("INVALID_RESOURCE")

    async def write(self, resource: Mapping[str, Any], payload: dict[str, Any]) -> Any:
        """写回一个资源的完整配置对象。"""
        if self._read_only:
            raise ConsoleError("READ_ONLY_MODE")
        kind = resource.get("kind")
        identifier = _quote(resource.get("id"))
        if kind == "provider":
            return await self.call(
                "PUT",
                "/api/v1/providers/by-id",
                {"provider_id": resource.get("id"), "config": payload},
            )
        if kind == "profile":
            return await self.call("PUT", f"/api/v1/config-profiles/{identifier}", payload)
        if kind == "plugin":
            return await self.call(
                "PUT",
                "/api/v1/plugins/config",
                {"plugin_id": resource.get("id"), "config": payload},
            )
        raise ConsoleError("INVALID_RESOURCE")

    # ------------------------------------------------------------------ 推理档位
    async def efforts(self, provider_id: str) -> list[Any] | None:
        """探测本地 OpenAI 兼容服务的推理档位；不可探测返回 ``None``。

        只有 ``api_base`` 指向 ``127.0.0.1`` / ``localhost`` 的 http 服务才探测，
        其余（含远程 API）一律视为「无法验证」返回 ``None``。
        """
        data = await self.call(
            "GET",
            f"/api/v1/providers/by-id?provider_id={_quote(provider_id)}&merged=true",
        )
        provider = data.get("provider") if isinstance(data, Mapping) else None
        if not isinstance(provider, Mapping):
            return None
        api_base = provider.get("api_base")
        if not isinstance(api_base, str) or not api_base:
            return None
        try:
            base = urlsplit(api_base)
        except ValueError:
            return None
        if base.scheme != "http" or base.hostname not in _LOCAL_HOSTS:
            return None
        keys = provider.get("key")
        api_key = keys[0] if isinstance(keys, list) and keys and isinstance(keys[0], str) else ""
        url = f"{api_base.rstrip('/')}/models?client_version=0.161.0"
        try:
            response = await self._client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=EFFORTS_TIMEOUT,
            )
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        rows = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(rows, list):
            rows = body.get("models") if isinstance(body, Mapping) else None
        rows = rows if isinstance(rows, list) else []
        model = next(
            (
                row
                for row in rows
                if isinstance(row, Mapping)
                and (row.get("id") or row.get("slug")) == provider.get("model")
            ),
            None,
        )
        if not isinstance(model, Mapping):
            return None
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list):
            return None
        return [
            level if isinstance(level, str) else (level.get("effort") if isinstance(level, Mapping) else None)
            for level in levels
        ]

    # ------------------------------------------------------------------ 资源清单
    async def providers(self) -> Any:
        """``GET /api/v1/providers`` 的 ``data``。"""
        return await self.call("GET", "/api/v1/providers")

    async def profiles(self) -> Any:
        """``GET /api/v1/config-profiles`` 的 ``data``（含 ``info_list``）。"""
        return await self.call("GET", "/api/v1/config-profiles")

    async def bots(self) -> tuple[Any, Any]:
        """返回 ``(bots, platform_stats)``；任一侧失败该侧为 ``None``。"""
        bots: Any = None
        stats: Any = None
        try:
            data = await self.call("GET", "/api/v1/bots")
            bots = data.get("bots") if isinstance(data, Mapping) else data
        except ConsoleError:
            bots = None
        try:
            data = await self.call("GET", "/api/v1/bots/stats")
            stats = data.get("platforms") if isinstance(data, Mapping) else None
        except ConsoleError:
            stats = None
        return bots, stats

    async def routes(self) -> Any:
        """``GET /api/v1/config-routes`` 的路由表（``routing``）。"""
        data = await self.call("GET", "/api/v1/config-routes")
        if isinstance(data, Mapping):
            return data.get("routing")
        return data

    async def routing_summary(self) -> dict[str, int]:
        """路由形态摘要；永不抛错，失败按全零返回。"""
        try:
            routing = await self.routes()
            if isinstance(routing, list):
                patterns = [item for item in routing if isinstance(item, str)]
            elif isinstance(routing, Mapping):
                patterns = [item for item in routing.keys() if isinstance(item, str)]
            else:
                patterns = []
            platform_level = sum(
                1 for pattern in patterns if _PLATFORM_LEVEL_ROUTE.match(pattern)
            )
            return {
                "total": len(patterns),
                "platformLevel": platform_level,
                "granular": len(patterns) - platform_level,
            }
        except Exception:  # noqa: BLE001 - 摘要永不打断快照
            return {"total": 0, "platformLevel": 0, "granular": 0}

    async def _plugin_config(self, plugin_id: str) -> Any:
        """读取单个插件配置对象（内部辅助）。"""
        data = await self.call(
            "GET", f"/api/v1/plugins/config?plugin_id={_quote(plugin_id)}"
        )
        return data.get("config") if isinstance(data, Mapping) else None

    async def plugin_settings(self) -> dict[str, Any]:
        """插件配置字典（``plugin_id -> config``）；读不到的插件不出现。"""
        data = await self.call("GET", "/api/v1/plugins")
        if isinstance(data, list):
            entries = data
        elif isinstance(data, Mapping):
            entries = data.get("plugins") or data.get("list") or []
        else:
            entries = []
        identifiers: list[str] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name") or entry.get("id") or entry.get("plugin_id")
            if isinstance(name, str) and name:
                identifiers.append(name)
        if not identifiers:
            return {}
        results = await asyncio.gather(
            *[self._plugin_config(identifier) for identifier in identifiers],
            return_exceptions=True,
        )
        settings: dict[str, Any] = {}
        for identifier, result in zip(identifiers, results):
            if isinstance(result, BaseException):
                continue
            settings[identifier] = result
        return settings

    async def _config_files(self) -> list[str]:
        """``data/config`` 下的 ``*_config.json`` 文件名。"""
        data_dir = await self._data_dir_path()
        return await asyncio.to_thread(_list_plugin_config_files, data_dir / "config")

    async def resources(self) -> list[dict[str, Any]]:
        """列出可审查资源：全局默认配置 + 其它配置档案 + 已装插件。"""
        listing, files, global_config = await asyncio.gather(
            self.profiles(),
            self._config_files(),
            self.read({"kind": "profile", "id": "default"}),
        )
        platforms: list[Any] = []
        if isinstance(global_config, Mapping):
            raw_platforms = global_config.get("platform")
            if isinstance(raw_platforms, list):
                platforms = raw_platforms
        routing: Any = {}
        try:
            routing = await self.routes()
        except ConsoleError:
            routing = {}
        routes = routing if isinstance(routing, Mapping) else {}
        try:
            data = await self.call("GET", "/api/v1/bots")
            bots = data.get("bots") if isinstance(data, Mapping) else None
            if isinstance(bots, list):
                platforms = bots
        except ConsoleError:
            pass
        stats: list[Any] = []
        try:
            data = await self.call("GET", "/api/v1/bots/stats")
            stats_value = data.get("platforms") if isinstance(data, Mapping) else None
            if isinstance(stats_value, list):
                stats = stats_value
        except ConsoleError:
            pass
        public_platforms = [
            {
                "id": platform.get("id"),
                "type": platform.get("type"),
                "status": next(
                    (
                        stat.get("status")
                        for stat in stats
                        if isinstance(stat, Mapping) and stat.get("id") == platform.get("id")
                    ),
                    "unknown",
                ),
            }
            for platform in platforms
            if isinstance(platform, Mapping)
        ]
        info_list = listing.get("info_list") if isinstance(listing, Mapping) else None
        info_list = info_list if isinstance(info_list, list) else []
        resources: list[dict[str, Any]] = [
            {
                "kind": "profile",
                "id": "default",
                "identity": profile_identity(
                    {"id": "default"}, public_platforms, routes
                ),
            }
        ]
        for item in info_list:
            if not isinstance(item, Mapping) or item.get("id") == "default":
                continue
            identifier = item.get("id") or item.get("uuid")
            resources.append(
                {
                    "kind": "profile",
                    "id": identifier,
                    "identity": profile_identity(item, public_platforms, routes),
                }
            )
        for name in files:
            resources.append({"kind": "plugin", "id": name[:-12]})
        return resources

    async def plugins(self) -> list[dict[str, Any]] | None:
        """脱敏插件清单（仅用于功能探测）；列表读不到时返回 ``None``。"""
        try:
            data = await self.call("GET", "/api/v1/plugins")
        except ConsoleError:
            return None
        if isinstance(data, list):
            entries = data
        elif isinstance(data, Mapping):
            entries = data.get("plugins") or data.get("list") or []
        else:
            entries = []
        inventory: list[dict[str, Any]] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name") or entry.get("id") or entry.get("plugin_id")
            if not isinstance(name, str) or not name:
                continue
            display_name = entry.get("display_name") or entry.get("displayName")
            version = entry.get("version")
            inventory.append(
                {
                    "name": name,
                    "displayName": display_name if isinstance(display_name, str) else None,
                    "version": version if isinstance(version, str) else None,
                    "activated": entry.get("activated") is True,
                }
            )
        return inventory
