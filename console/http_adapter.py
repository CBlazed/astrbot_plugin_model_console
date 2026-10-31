"""远程 AstrBot 只读适配器（OpenAPI v1 + scoped ``abk_`` Key）。

对齐旧 Node 实现 ``model-console/src/http-adapter.mjs`` 与 ``engine.mjs`` 的远程快照口径：

* 只允许 ``/api/v1/`` 路径；Key 只放在 ``Authorization`` 头里，绝不出现在 URL / 错误信息 / 快照中；
* HTTP 失败映射成稳定错误码：``DEVICE_UNREACHABLE`` / ``DEVICE_AUTH_FAILED`` /
  ``DEVICE_FORBIDDEN`` / ``DEVICE_HTTP_<status>`` / ``DEVICE_INVALID_RESPONSE`` /
  ``DEVICE_API_REJECTED``；
* :meth:`HttpGatewayAdapter.snapshot` 按旧 ``Engine.loadSnapshot`` 的字段组装远程快照
  （``readAt/writeEnabled/configVersion/coverage/routing/plugins/features/models/resources``），
  远程视图恒为只读（``writeEnabled=False``）；
* 远程端的 profile/plugin 身份文案用旧实现的「远程」口径。

reviewed 字段表统一从 :mod:`console.registry` 取（单一数据源，避免重复实现分叉）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from . import registry as _registry
from .errors import ConsoleError

_PLATFORM_ROUTE = re.compile(r"^[^:]+::$")
_EMBEDDING = re.compile(r"embedding")
_RERANK = re.compile(r"rerank")
_STT = re.compile(r"stt")
_TTS = re.compile(r"tts")
_LLM = re.compile(r"chat_completion|responses")


def adapter_error(code: str, message: str = "") -> ConsoleError:
    """构造远程适配器错误（旧实现 ``adapterError``）。"""
    return ConsoleError(code, message)


# reviewed 字段表来自 console.registry（单一数据源）。


def _js_string(value: Any) -> str:
    """模拟 JS ``String(value)``（覆盖快照里会出现的标量与容器）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, list):
        return ",".join(_js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _canonical(value: Any) -> Any:
    """按键名递归排序，供修订号计算使用（旧实现 ``canonical``）。"""
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    return value


def revision(value: Any) -> str:
    """对象修订号：规范化 JSON 的 SHA-256（旧实现 ``revision``）。"""
    encoded = json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fields(resource: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按资源类型返回 reviewed 字段表（复用 console.registry，保持单一数据源）。"""
    return _registry.field_table(resource.get("kind"), resource.get("id"))


def infer_capability(kind: Any = "") -> list[str]:
    """从 provider/source 的 type 推断能力（旧实现 ``inferCapability``）。"""
    text = kind if isinstance(kind, str) else ""
    if _EMBEDDING.search(text):
        return ["embedding"]
    if _RERANK.search(text):
        return ["rerank"]
    if _STT.search(text):
        return ["stt"]
    if _TTS.search(text):
        return ["tts"]
    if _LLM.search(text):
        return ["llm"]
    return []


def _config_version_of(config: Any) -> Any:
    """默认档案上的 ``config_version``；缺失或非标量视为未知（None）。"""
    value = config.get("config_version") if isinstance(config, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    return None


def _models_from_profile(profile_config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从默认档案的 provider/provider_sources 组装远程模型列表。"""
    providers = profile_config.get("provider") if isinstance(profile_config, dict) else None
    sources = profile_config.get("provider_sources") if isinstance(profile_config, dict) else None
    source_list = sources if isinstance(sources, list) else []
    models: list[dict[str, Any]] = []
    for provider in providers if isinstance(providers, list) else []:
        if not isinstance(provider, dict):
            continue
        source = next((item for item in source_list
                       if isinstance(item, dict)
                       and item.get("id") == provider.get("provider_source_id")), None) or {}
        capabilities = infer_capability(source.get("type") if source.get("type") is not None
                                        else provider.get("type"))
        modalities = provider.get("modalities")
        if "llm" in capabilities and isinstance(modalities, list) and "image" in modalities:
            capabilities.append("vision")
        effort = None
        extra = provider.get("custom_extra_body")
        if isinstance(extra, dict):
            effort = extra.get("reasoning_effort")
        models.append({
            "revision": revision(provider),
            "id": provider.get("id"),
            "model": provider.get("model"),
            "enabled": provider.get("enable") is not False,
            "capabilities": capabilities,
            "capabilityEvidence": "configured provider type/modalities, not generation tested",
            "effort": effort,
        })
    return models


def _binding_rows(resource: Mapping[str, Any], config: Mapping[str, Any],
                  models: list[dict[str, Any]], session_model: Any = None,
                  features: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """生成一个资源的绑定行表（旧实现 ``bindingRows``）。"""
    rows: list[dict[str, Any]] = []
    for field in _fields(resource):
        path = field["path"]
        present = _registry.has_path(config, path)
        if field["capability"] == "setting":
            raw = _registry.get_path(config, path) if present else None
            kind = field.get("type")
            if kind == "bool":
                if present:
                    if raw is True:
                        value = "true"
                    elif raw is False:
                        value = "false"
                    else:
                        value = _js_string(raw)
                else:
                    value = ""
                rows.append({**resource, **field, "value": value,
                             "status": "field-missing" if not present
                             else ("configured" if value == "true" else "off")})
                continue
            if kind in ("secret-list", "secret-string"):
                if isinstance(raw, list):
                    count = sum(1 for item in raw
                                if isinstance(item, str) and item.strip())
                elif isinstance(raw, str) and raw.strip():
                    count = 1
                else:
                    count = 0
                rows.append({**resource, **field, "configured": count > 0, "count": count,
                             "status": "field-missing" if not present
                             else ("configured" if count > 0 else "empty")})
                continue
            if kind == "model":
                value = _js_string(raw if raw is not None else "") if present else ""
                options = [{"value": model.get("id"), "label": model.get("id")}
                           for model in models
                           if model.get("enabled") and "llm" in (model.get("capabilities") or [])]
                selected = next((model for model in models
                                 if model.get("id") == value), None) if value else None
                if not value:
                    status = "configured"
                elif selected:
                    status = "disabled" if not selected.get("enabled") else "configured"
                else:
                    status = "missing"
                rows.append({**resource, **field, "value": value, "options": options,
                             "status": status})
                continue
            value = _js_string(raw if raw is not None else "") if present else ""
            options = _registry.web_search_provider_options(features)
            selected = next((option for option in options
                             if option.get("value") == value), None)
            status = "field-missing" if not present else "configured"
            if value == "model_native" and (selected is None or selected.get("disabled")):
                status = "plugin-unavailable"
            elif value and selected is None:
                status = "value-unrecognized"
            rows.append({**resource, **field, "value": value, "options": options,
                         "status": status})
            continue
        raw_binding = _registry.get_path(config, path) if present else None
        value = ("" if raw_binding is None else raw_binding) if present else ""
        resolved = value or (session_model if field.get("inherit") == "session" else None)
        model = next((item for item in models if item.get("id") == resolved), None)
        if not present:
            status = "field-missing"
        elif resolved:
            if model is None:
                status = "missing"
            elif not model.get("enabled"):
                status = "disabled"
            else:
                status = "configured"
        elif field.get("inherit") == "session":
            status = "per-session"
        else:
            status = "empty-semantics-unverified"
        rows.append({**resource, **field, "value": value, "effectiveModel": resolved,
                     "status": status})
    return rows


def _features_from_plugins(plugins: Any) -> dict[str, Any]:
    """从脱敏插件清单推导原生搜索能力（旧实现 ``featuresFromPlugins``）。"""
    listing = plugins if isinstance(plugins, list) else None
    native = None
    if listing is not None:
        native = next((item for item in listing
                       if isinstance(item, dict)
                       and item.get("name") == "astrbot_plugin_model_websearch"), None)
    activated = bool(native.get("activated")) if native else False
    return {
        "nativeWebSearch": {
            "pluginId": "astrbot_plugin_model_websearch",
            "listAvailable": isinstance(plugins, list),
            "installed": native is not None,
            "activated": activated,
            "available": activated,
            "version": native.get("version") if native else None,
        },
    }


def remote_identity(identity_id: Any, name: Any, routing: Mapping[str, Any] | None) -> dict[str, Any]:
    """远程 profile/plugin 的身份文案（旧实现 ``remoteIdentity``）。"""
    granular = int((routing or {}).get("granular") or 0)
    if identity_id == "default":
        display_name = f"{name or '全局默认配置'}（远程）"
    else:
        display_name = name or f"远程配置 · {str(identity_id)[:8]}"
    return {
        "name": display_name,
        "transport": "远程设备",
        "association": "remote-default" if identity_id == "default" else "remote-profile",
        "instances": [],
        "account": None,
        "nickname": None,
        "routingStatus": ("远程视图：存在会话级/通配符路由，仅平台级展示" if granular > 0
                          else "远程只读视图"),
    }


class HttpGatewayAdapter:
    """远程 AstrBot 的只读网关适配器（OpenAPI v1 + API Key）。"""

    def __init__(self, *, base_url: str, api_key: str,
                 client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout_ms: int = 10000) -> None:
        """创建适配器；``client`` / ``transport`` 是测试注入点。

        Raises:
            ConsoleError: ``DEVICE_INVALID``（缺 baseUrl）/ ``DEVICE_SECRET_REQUIRED``（缺 Key）。
        """
        if not isinstance(base_url, str) or not base_url:
            raise adapter_error("DEVICE_INVALID", "缺少 baseUrl")
        if not isinstance(api_key, str) or not api_key:
            raise adapter_error("DEVICE_SECRET_REQUIRED", "缺少 OpenAPI Key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_ms = int(timeout_ms) if timeout_ms else 10000
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=max(0.001, self.timeout_ms / 1000.0),
                transport=transport,
                follow_redirects=False,
            )
        self._client = client

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        """调用远程 OpenAPI；返回响应里的 ``data`` 字段。

        Raises:
            ConsoleError: 稳定错误码（见模块 docstring）。
        """
        if not isinstance(path, str) or not path.startswith("/api/v1/"):
            raise adapter_error("DEVICE_INVALID", "只允许 /api/v1/ 路径")
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["content"] = json.dumps(body).encode("utf-8")
        try:
            response = await self._client.request(
                method, f"{self.base_url}{path}", **kwargs)
        except Exception:  # noqa: BLE001 - 网络错误统一不可达
            raise adapter_error("DEVICE_UNREACHABLE", "远程 AstrBot 不可达") from None
        if response.is_redirect:
            raise adapter_error("DEVICE_UNREACHABLE", "远程 AstrBot 不可达")
        status = response.status_code
        if status == 401:
            raise adapter_error("DEVICE_AUTH_FAILED", "远程设备凭据无效或已被撤销")
        if status == 403:
            raise adapter_error("DEVICE_FORBIDDEN", "远程设备缺少所需 scope")
        if status < 200 or status >= 300:
            raise adapter_error(f"DEVICE_HTTP_{status}", f"远程 AstrBot 返回 HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            raise adapter_error("DEVICE_INVALID_RESPONSE",
                                "远程响应不是合法 JSON") from None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise adapter_error("DEVICE_API_REJECTED", "远程 AstrBot 拒绝了请求")
        return payload.get("data")

    async def read(self, resource: Mapping[str, Any]) -> Any:
        """读一个资源（provider / profile / plugin）的完整配置对象。"""
        identity = quote(str((resource or {}).get("id")), safe="")
        kind = (resource or {}).get("kind")
        if kind == "provider":
            data = await self.call("GET", f"/api/v1/providers/by-id?provider_id={identity}")
            return data.get("provider") if isinstance(data, dict) else None
        if kind == "profile":
            data = await self.call("GET", f"/api/v1/config-profiles/{identity}")
            return data.get("config") if isinstance(data, dict) else None
        if kind == "plugin":
            data = await self.call("GET", f"/api/v1/plugins/config?plugin_id={identity}")
            return data.get("config") if isinstance(data, dict) else None
        raise adapter_error("DEVICE_INVALID", "未知资源类型")

    async def write(self, resource: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
        """写一个资源（远程分支实际只用只读，保留接口对齐旧实现）。"""
        identity = quote(str((resource or {}).get("id")), safe="")
        kind = (resource or {}).get("kind")
        if kind == "provider":
            return await self.call("PUT", "/api/v1/providers/by-id",
                                   {"provider_id": resource.get("id"), "config": config})
        if kind == "profile":
            return await self.call("PUT", f"/api/v1/config-profiles/{identity}", config)
        if kind == "plugin":
            return await self.call("PUT", "/api/v1/plugins/config",
                                   {"plugin_id": resource.get("id"), "config": config})
        raise adapter_error("DEVICE_INVALID", "未知资源类型")

    async def efforts(self, provider_id: str) -> list[str] | None:
        """远程推理档位探测不在 scoped OpenAPI 里，返回 None（与旧实现一致）。"""
        return None

    async def routing_summary(self) -> dict[str, int]:
        """路由形态摘要；任何失败都回落全零。"""
        try:
            data = await self.call("GET", "/api/v1/config-routes")
            routing = data.get("routing") if isinstance(data, dict) else None
            patterns = list(routing.keys()) if isinstance(routing, dict) else []
            platform_level = sum(1 for pattern in patterns
                                 if isinstance(pattern, str) and _PLATFORM_ROUTE.match(pattern))
            return {"total": len(patterns), "platformLevel": platform_level,
                    "granular": len(patterns) - platform_level}
        except Exception:  # noqa: BLE001 - 旧实现静默回落
            return {"total": 0, "platformLevel": 0, "granular": 0}

    async def resources(self) -> list[dict[str, Any]]:
        """远程资源清单：default 档案 + 其它档案 + 插件（旧实现 ``resources``）。"""
        routing = await self.routing_summary()
        data = await self.call("GET", "/api/v1/config-profiles")
        info_list = data.get("info_list") if isinstance(data, dict) else None
        profile_list = info_list if isinstance(info_list, list) else []
        profiles: list[dict[str, Any]] = []
        for item in profile_list:
            if not isinstance(item, dict):
                continue
            identity_id = item.get("id") if item.get("id") is not None else item.get("uuid")
            if identity_id == "default":
                continue
            profiles.append({
                "kind": "profile",
                "id": identity_id,
                "identity": remote_identity(identity_id, item.get("name"), routing),
            })
        plugins: list[dict[str, Any]] = []
        try:
            plugin_data = await self.call("GET", "/api/v1/plugins")
            if isinstance(plugin_data, list):
                items = plugin_data
            elif isinstance(plugin_data, dict):
                items = plugin_data.get("plugins") or plugin_data.get("list") or []
            else:
                items = []
            for plugin in items if isinstance(items, list) else []:
                if not isinstance(plugin, dict):
                    continue
                identity_id = plugin.get("name") or plugin.get("id") or plugin.get("plugin_id")
                if isinstance(identity_id, str) and identity_id:
                    plugins.append({"kind": "plugin", "id": identity_id})
        except Exception:  # noqa: BLE001 - plugins scope 可选
            pass
        return [
            {"kind": "profile", "id": "default",
             "identity": remote_identity("default", "全局默认配置", routing)},
            *profiles,
            *plugins,
        ]

    async def health(self) -> dict[str, Any]:
        """连通性探测：``{reachable, latencyMs, error?}``（旧实现 ``health``）。"""
        started = time.monotonic()
        try:
            await self.call("GET", "/api/v1/config-profiles")
            return {"reachable": True,
                    "latencyMs": int((time.monotonic() - started) * 1000)}
        except Exception as error:  # noqa: BLE001
            code = error.code if isinstance(error, ConsoleError) else "DEVICE_UNREACHABLE"
            return {"reachable": False,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                    "error": code}

    async def plugins(self) -> list[dict[str, Any]] | None:
        """脱敏插件清单（只用于能力探测，绝不返回凭据）；读不到返回 None。"""
        try:
            data = await self.call("GET", "/api/v1/plugins")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("plugins") or data.get("list") or []
            else:
                items = []
            result: list[dict[str, Any]] = []
            for plugin in items if isinstance(items, list) else []:
                if not isinstance(plugin, dict):
                    continue
                name = plugin.get("name") or plugin.get("id") or plugin.get("plugin_id")
                if not isinstance(name, str) or not name:
                    continue
                display = plugin.get("display_name")
                if display is None:
                    display = plugin.get("displayName")
                version = plugin.get("version")
                result.append({
                    "name": name,
                    "displayName": display,
                    "version": version if isinstance(version, str) else None,
                    "activated": plugin.get("activated") is True,
                })
            return result
        except Exception:  # noqa: BLE001
            return None

    async def snapshot(self) -> dict[str, Any]:
        """拼装远程只读快照（字段与旧 ``Engine.loadSnapshot`` 一致）。"""
        profile = await self.read({"kind": "profile", "id": "default"})
        config = profile if isinstance(profile, dict) else {}
        models = _models_from_profile(config)
        resources = await self.resources()
        routing = await self.routing_summary()
        plugins = await self.plugins()
        features = _features_from_plugins(plugins)
        results: list[dict[str, Any]] = []
        for resource in resources:
            try:
                resource_config = await self.read(resource)
                resource_config = resource_config if isinstance(resource_config, dict) else {}
                fields = _fields(resource)
                results.append({
                    "resource": resource,
                    "revision": revision(resource_config),
                    "bindings": _binding_rows(resource, resource_config, models, None, features),
                    "unreviewed": [
                        {"path": item["path"], "reviewRequired": True}
                        for item in _registry.discover(resource_config)
                        if not any(field.get("path") == item["path"] for field in fields)
                    ],
                })
            except Exception:  # noqa: BLE001 - 单个资源不可读不影响整体快照
                results.append({"resource": resource, "status": "unavailable"})
        return {
            "readAt": _now_iso(),
            "writeEnabled": False,
            "configVersion": _config_version_of(config),
            "coverage": "reviewed bindings plus unreviewed field discovery; hidden schema/defaults not guaranteed",
            "routing": routing,
            "plugins": plugins if isinstance(plugins, list) else None,
            "features": features,
            "models": [
                {**model,
                 "resource": {"kind": "provider", "id": model.get("id")},
                 "writableFields": ["custom_extra_body.reasoning_effort"]}
                for model in models
            ],
            "resources": results,
        }

    async def close(self) -> None:
        """释放自建的 HTTP 客户端（注入的客户端由调用方负责）。"""
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass


def _now_iso() -> str:
    """当前 UTC 时间，格式与 ``Date.prototype.toISOString`` 一致。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
