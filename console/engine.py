"""计划 / 应用引擎（旧实现 ``model-console/src/engine.mjs`` 的 Python 移植）。

语义照旧：
* 一份计划只针对**一个资源**；不承诺跨资源原子性。
* 计划保存「写前完整对象」「写后完整对象」「写前规范化 revision」，应用时：
  revision 双校验（计划生成时 + 备份后）→ 写前备份 → 审计 ``apply-start`` → 写入 →
  回读比对 ``VERIFY_FAILED`` → 审计 ``applied``；任何失败都审计 ``apply-failed``。
* Secret 字段值（网页搜索 API Key）永不出现在计划响应里，只给计数摘要。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import backup as backup_module
from . import registry
from .errors import ConsoleError, known_error

#: 一份计划最多容纳的变更条数。
MAX_PLAN_CHANGES = 30
#: 计划表上限（防止无界增长）。
MAX_PLANS = 100
#: 计划有效期（毫秒）。
PLAN_TTL_MS = 10 * 60 * 1000
#: 快照缓存有效期（毫秒）。
SNAPSHOT_TTL_MS = 2000
#: secret-list 的 Key 数量与长度上限。
MAX_SECRET_KEYS = 20
MAX_SECRET_LENGTH = 4096
#: 计划响应里的通用警告（与旧实现逐字一致）。
PLAN_WARNING = "No cross-resource atomicity; API readback is not a model generation test"
#: 恢复计划的警告文案（与旧实现逐字一致）。
RESTORE_WARNING = (
    "恢复会覆盖该资源完整对象：备份中的配置将整体写回，只有列出的已审查字段差异经过校验。"
    "应用后可读回验证，但这不是模型生成测试。"
)


def iso_now() -> str:
    """当前 UTC 时间，格式与 ``Date#toISOString`` 一致。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _iso_from_ms(milliseconds: int) -> str:
    """毫秒时间戳 → ISO 字符串。"""
    return (
        datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def canonicalize(value: Any) -> Any:
    """规范化 JSON 值：对象键递归排序，数组保持顺序（等价旧 ``canonical``）。"""
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, Mapping):
        return {key: canonicalize(value[key]) for key in sorted(value.keys(), key=str)}
    return value


def revision(value: Any) -> str:
    """配置对象的稳定 revision：规范化 JSON 的 SHA-256（等价旧 ``revision``）。"""
    payload = json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def backup_short_id(value: Any) -> str:
    """把备份引用收敛成目录名（uuid）。"""
    if not isinstance(value, str):
        return ""
    parts = [part for part in value.replace("/", "\\").split("\\") if part]
    return parts[-1] if parts else ""


def config_version_of(config: Any) -> Any:
    """取默认档案上的 ``config_version``；缺失或非标量视为未知（``None``）。"""
    if not isinstance(config, Mapping):
        return None
    value = config.get("config_version")
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def _js_string(value: Any) -> str:
    """近似 ``String(value)``：布尔/None 按 JS 语义，其余用 JSON 或 ``str``。"""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _nullish_empty(value: Any) -> Any:
    """``value ?? ''`` 的等价物。"""
    return "" if value is None else value


def infer_capability(provider_type: Any) -> list[str]:
    """按 provider 类型推断能力（与旧 ``inferCapability`` 逐字对应）。"""
    text = provider_type if isinstance(provider_type, str) else ""
    if "embedding" in text:
        return ["embedding"]
    if "rerank" in text:
        return ["rerank"]
    if "stt" in text:
        return ["stt"]
    if "tts" in text:
        return ["tts"]
    if "chat_completion" in text or "responses" in text:
        return ["llm"]
    return []


def binding_rows(
    resource: Mapping[str, Any],
    config: Any,
    models: list[Mapping[str, Any]],
    session_model: Any = None,
    features: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """生成某资源绑定行的展示数据（快照核心结构，字段名与旧实现一致）。"""
    rows: list[dict[str, Any]] = []
    base = {
        "kind": resource.get("kind"),
        "id": resource.get("id"),
    }
    for field in registry.field_table(resource.get("kind") or "", resource.get("id")):
        present = registry.has_path(config, field["path"])
        row = {**base, **field}
        path = field["path"]
        if field.get("capability") == "setting":
            raw = registry.get_path(config, path) if present else None
            field_type = field.get("type")
            if field_type == "bool":
                if not present:
                    value = ""
                elif raw is True:
                    value = "true"
                elif raw is False:
                    value = "false"
                else:
                    value = _js_string(raw)
                row.update(
                    value=value,
                    status="field-missing" if not present else ("configured" if value == "true" else "off"),
                )
                rows.append(row)
                continue
            if field_type in ("secret-list", "secret-string"):
                if isinstance(raw, list):
                    count = sum(
                        1
                        for item in raw
                        if isinstance(item, str) and item.strip()
                    )
                elif isinstance(raw, str) and raw.strip():
                    count = 1
                else:
                    count = 0
                row.update(
                    configured=count > 0,
                    count=count,
                    status="field-missing" if not present else ("configured" if count > 0 else "empty"),
                )
                rows.append(row)
                continue
            if field_type == "model":
                value = _js_string(raw if raw is not None else "") if present else ""
                options = [
                    {"value": model.get("id"), "label": model.get("id")}
                    for model in models
                    if model.get("enabled") and "llm" in (model.get("capabilities") or [])
                ]
                selected = (
                    next((model for model in models if model.get("id") == value), None)
                    if value
                    else None
                )
                if not value:
                    status = "configured"
                elif selected is None:
                    status = "missing"
                elif not selected.get("enabled"):
                    status = "disabled"
                else:
                    status = "configured"
                row.update(value=value, options=options, status=status)
                rows.append(row)
                continue
            value = _js_string(raw if raw is not None else "") if present else ""
            options = registry.web_search_provider_options(features)
            selected = (
                next((option for option in options if option.get("value") == value), None)
                if value
                else None
            )
            status = "field-missing" if not present else "configured"
            if value == "model_native" and (selected is None or selected.get("disabled")):
                status = "plugin-unavailable"
            elif value and selected is None:
                status = "value-unrecognized"
            row.update(value=value, options=options, status=status)
            rows.append(row)
            continue
        value = _nullish_empty(registry.get_path(config, path)) if present else ""
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
        row.update(value=value, effectiveModel=resolved, status=status)
        rows.append(row)
    return rows


def parse_secret_list(value: Any) -> list[str] | None:
    """解析 secret-list 变更（UI 的 JSON 字符串或备份恢复的数组）。"""
    candidate = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            candidate = json.loads(text)
        except ValueError:
            return None
    if not isinstance(candidate, list):
        return None
    keys: list[str] = []
    for item in candidate:
        text = item.strip() if isinstance(item, str) else ""
        if text:
            keys.append(text)
    if len(keys) > MAX_SECRET_KEYS:
        return None
    if any(len(key) > MAX_SECRET_LENGTH for key in keys):
        return None
    return keys


def masked_secret_summary(path: str, value: Any) -> str:
    """Secret 变更的展示摘要（只给数量，不给值）。"""
    field = next(
        (item for item in registry.field_table("profile") if item["path"] == path),
        None,
    )
    if field and field.get("type") == "secret-list":
        keys = parse_secret_list(value)
        count = len(keys) if isinstance(keys, list) else 0
        return f"设置 {count} 个 Key（已隐藏）" if count > 0 else "清空 Key"
    return "设置 Key（已隐藏）" if isinstance(value, str) and value.strip() else "清空 Key"


def display_changes(changes: Any) -> list[Any]:
    """把计划/差异里的 Secret 值替换成掩码摘要。"""
    result: list[Any] = []
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, Mapping) or not registry.is_secret_path(change.get("path")):
            result.append(change)
            continue
        masked = dict(change)
        masked["value"] = "（已隐藏）"
        masked["secret"] = True
        masked["summary"] = masked_secret_summary(str(change["path"]), change.get("value"))
        result.append(masked)
    return result


def restore_changes(
    resource: Mapping[str, Any],
    before: Any,
    after: Any,
) -> list[dict[str, Any]]:
    """生成恢复差异：只比较已审查字段（恢复本身仍写完整对象）。"""

    def as_text(value: Any) -> str:
        """设置值统一转文本。"""
        if value is True:
            return "true"
        if value is False:
            return "false"
        if value is None:
            return ""
        return str(value)

    kind = resource.get("kind")
    if kind == "provider":
        path = "custom_extra_body.reasoning_effort"
        before_value = registry.get_path(before, path)
        before_value = "" if before_value is None else before_value
        value = registry.get_path(after, path)
        value = "" if value is None else value
        if before_value == value:
            return []
        return [{"path": path, "before": before_value, "value": value, "capability": "llm"}]
    changes: list[dict[str, Any]] = []
    for field in registry.field_table(kind or "", resource.get("id")):
        path = field["path"]
        before_raw = registry.get_path(before, path)
        after_raw = registry.get_path(after, path)
        if field.get("capability") == "setting":
            before_value = as_text(before_raw)
            value = as_text(after_raw)
        else:
            before_value = _nullish_empty(before_raw)
            value = _nullish_empty(after_raw)
        if before_value == value:
            continue
        changes.append(
            {
                "path": path,
                "before": before_value,
                "value": value,
                "capability": field.get("capability"),
            }
        )
    return changes


def _impact_of(resource: Mapping[str, Any]) -> str:
    """计划影响面文案（与旧实现逐字一致）。"""
    kind = resource.get("kind")
    if kind == "provider":
        return "all references to this shared model"
    if kind == "plugin":
        return "all bots using this plugin"
    return "this profile"


class Engine:
    """单资源计划 / 应用引擎（带快照缓存与写序列化）。"""

    def __init__(
        self,
        gateway: Any,
        state_dir: Path,
        data_dir: Path,
        limits: Mapping[str, int] | None = None,
        read_only: bool = True,
        *,
        ttl_ms: int = SNAPSHOT_TTL_MS,
    ) -> None:
        """创建引擎。

        Args:
            gateway: :class:`console.gateway.Gateway` 或同形对象。
            state_dir: 状态目录（审计、备份）。
            data_dir: AstrBot data 目录（备份源）。
            limits: 备份保留上限 ``{"keep","maxBytes"}``。
            read_only: 只读模式（``True`` 时 :meth:`apply` 直接拒绝）。
            ttl_ms: 快照缓存有效期（毫秒）。
        """
        self.gateway = gateway
        self.state_dir = Path(state_dir)
        self.data_dir = Path(data_dir)
        self.limits: dict[str, int] = dict(
            limits or {"keep": backup_module.DEFAULT_BACKUP_KEEP, "maxBytes": backup_module.DEFAULT_BACKUP_MAX_MB * backup_module.MEBIBYTE}
        )
        self.read_only = bool(read_only)
        self.ttl_ms = int(ttl_ms)
        self.plans: dict[str, dict[str, Any]] = {}
        self._busy = False
        self._cache: dict[str, Any] | None = None
        self._pending: asyncio.Future[Any] | None = None

    # ------------------------------------------------------------------ 只读视图
    @property
    def write_enabled(self) -> bool:
        """是否允许写入（= 非只读）。"""
        return not self.read_only

    def set_read_only(self, value: bool) -> None:
        """切换只读标记。"""
        self.read_only = bool(value)

    async def models(self, config: Any = None) -> list[dict[str, Any]]:
        """从全局默认配置推导模型库（能力由 provider 类型/modalities 推断，未做生成测试）。"""
        global_config = config
        if not isinstance(global_config, Mapping):
            global_config = await self.gateway.read({"kind": "profile", "id": "default"})
        providers = global_config.get("provider") if isinstance(global_config, Mapping) else None
        providers = providers if isinstance(providers, list) else []
        sources = (
            global_config.get("provider_sources")
            if isinstance(global_config, Mapping)
            else None
        )
        sources = sources if isinstance(sources, list) else []
        result: list[dict[str, Any]] = []
        for provider in providers:
            if not isinstance(provider, Mapping):
                continue
            source = next(
                (
                    item
                    for item in sources
                    if isinstance(item, Mapping)
                    and item.get("id") == provider.get("provider_source_id")
                ),
                None,
            )
            source_type = source.get("type") if isinstance(source, Mapping) else None
            provider_type = source_type if source_type is not None else provider.get("type")
            capabilities = infer_capability(provider_type)
            modalities = provider.get("modalities")
            if (
                "llm" in capabilities
                and isinstance(modalities, list)
                and "image" in modalities
            ):
                capabilities.append("vision")
            extra = provider.get("custom_extra_body")
            effort = extra.get("reasoning_effort") if isinstance(extra, Mapping) else None
            result.append(
                {
                    "revision": revision(provider),
                    "id": provider.get("id"),
                    "model": provider.get("model"),
                    "enabled": provider.get("enable") is not False,
                    "capabilities": capabilities,
                    "capabilityEvidence": "configured provider type/modalities, not generation tested",
                    "effort": effort,
                }
            )
        return result

    def features_from_plugins(self, plugins: Any) -> dict[str, Any]:
        """由脱敏插件清单推导功能开关；``None`` 表示清单不可读。"""
        plugin_list = plugins if isinstance(plugins, list) else None
        native = None
        if plugin_list is not None:
            native = next(
                (
                    plugin
                    for plugin in plugin_list
                    if isinstance(plugin, Mapping)
                    and plugin.get("name") == registry.NATIVE_SEARCH_PLUGIN["id"]
                ),
                None,
            )
        activated = bool(native.get("activated")) if isinstance(native, Mapping) else False
        version = native.get("version") if isinstance(native, Mapping) else None
        return {
            "nativeWebSearch": {
                "pluginId": registry.NATIVE_SEARCH_PLUGIN["id"],
                "listAvailable": isinstance(plugin_list, list),
                "installed": native is not None,
                "activated": activated,
                "available": activated,
                "version": version if isinstance(version, str) else None,
            }
        }

    async def native_search_features(self) -> dict[str, Any]:
        """计划时重新读取插件清单，复核原生搜索可用性。"""
        try:
            plugins = await self.gateway.plugins()
        except Exception:  # noqa: BLE001 - 复核失败按「未安装」处理
            plugins = None
        return self.features_from_plugins(plugins)

    async def routing_summary(self) -> dict[str, int]:
        """路由摘要；gateway 不提供或失败时返回全零。"""
        summary_method = getattr(self.gateway, "routing_summary", None)
        if summary_method is None:
            return {"total": 0, "platformLevel": 0, "granular": 0}
        try:
            summary = await summary_method()
        except Exception:  # noqa: BLE001
            return {"total": 0, "platformLevel": 0, "granular": 0}
        if not isinstance(summary, Mapping):
            return {"total": 0, "platformLevel": 0, "granular": 0}
        return {
            "total": summary.get("total") if isinstance(summary.get("total"), int) else 0,
            "platformLevel": summary.get("platformLevel")
            if isinstance(summary.get("platformLevel"), int)
            else 0,
            "granular": summary.get("granular") if isinstance(summary.get("granular"), int) else 0,
        }

    # ------------------------------------------------------------------ 快照
    async def snapshot(self) -> dict[str, Any]:
        """返回配置快照（默认缓存 2 秒，同一时刻只加载一次）。"""
        cached = self._cache
        if cached is not None and _now_ms() - cached["at"] < self.ttl_ms:
            return cached["value"]
        pending = self._pending
        if pending is not None:
            return await asyncio.shield(pending)
        task = asyncio.ensure_future(self._load_snapshot())
        self._pending = task
        try:
            value = await task
        finally:
            self._pending = None
        self._cache = {"at": _now_ms(), "value": value}
        return value

    async def _plugins_or_none(self) -> Any:
        """读插件清单；失败返回 ``None``（不打断快照）。"""
        try:
            return await self.gateway.plugins()
        except Exception:  # noqa: BLE001
            return None

    async def _load_snapshot(self) -> dict[str, Any]:
        """实际加载快照：先并行拉全局配置/资源清单/路由/插件，再并行读每个资源。"""
        default_resource = {"kind": "profile", "id": "default"}
        global_config, resources, routing, plugins = await asyncio.gather(
            self.gateway.read(default_resource),
            self.gateway.resources(),
            self.routing_summary(),
            self._plugins_or_none(),
        )
        models = await self.models(global_config)
        features = self.features_from_plugins(plugins)

        async def read_one(resource: Mapping[str, Any]) -> dict[str, Any]:
            """读一个资源并生成绑定视图；失败标记 unavailable。"""
            try:
                config = await self.gateway.read(resource)
            except Exception:  # noqa: BLE001 - 单个资源失败不影响整份快照
                return {"resource": resource, "status": "unavailable"}
            bindings = binding_rows(resource, config, models, None, features)
            unreviewed = registry.build_bindings(
                config,
                kind=resource.get("kind") or "",
                resource_id=resource.get("id"),
            )["unreviewed"]
            return {
                "resource": resource,
                "revision": revision(config),
                "bindings": bindings,
                "unreviewed": unreviewed,
            }

        results = await asyncio.gather(*[read_one(resource) for resource in resources])
        return {
            "readAt": iso_now(),
            "writeEnabled": self.write_enabled,
            "configVersion": config_version_of(global_config),
            "coverage": "reviewed bindings plus unreviewed field discovery; hidden schema/defaults not guaranteed",
            "routing": routing,
            "plugins": plugins if isinstance(plugins, list) else None,
            "features": features,
            "models": [
                {
                    **model,
                    "resource": {"kind": "provider", "id": model.get("id")},
                    "writableFields": ["custom_extra_body.reasoning_effort"],
                }
                for model in models
            ],
            "resources": results,
        }

    # ------------------------------------------------------------------ 计划
    def _purge_expired(self) -> None:
        """丢弃过期计划。"""
        now = _now_ms()
        for key in [key for key, plan in self.plans.items() if plan["expires"] < now]:
            self.plans.pop(key, None)

    async def plan(
        self,
        resource: Mapping[str, Any],
        changes: list[dict[str, Any]],
        restore: bool = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """生成一份计划。

        Args:
            resource: ``{"kind","id"}``。
            changes: ``[{"path","value"}]``，明文 Secret 只在内存里流转。
            restore: ``True`` 表示内部复核（apply 的二次校验），不登记进计划表；
                恢复计划请用 :meth:`plan_restore`。
            expected_revision: 客户端持有的 revision，不一致抛 ``CONCURRENT_CHANGE``。

        Returns:
            计划视图（id/resource/revision/changes/expiresAt/impact/warning）。

        Raises:
            ConsoleError: ``INVALID_PLAN`` / ``PLAN_LIMIT`` / ``INVALID_CHANGE`` /
                ``FIELD_NOT_WRITABLE`` / ``CONCURRENT_CHANGE`` / ``MODEL_MISSING_OR_DISABLED`` /
                ``CAPABILITY_UNVERIFIED_OR_MISMATCH`` / ``VALUE_NOT_ALLOWED`` /
                ``SECRET_VALUE_INVALID`` / ``EFFORT_UNVERIFIED_OR_UNSUPPORTED`` 等。
        """
        if (
            not isinstance(resource, Mapping)
            or resource.get("kind") not in registry.RESOURCE_KINDS
            or not isinstance(resource.get("id"), str)
            or not isinstance(changes, list)
            or not changes
            or len(changes) > MAX_PLAN_CHANGES
        ):
            raise ConsoleError("INVALID_PLAN")
        self._purge_expired()
        if not restore and len(self.plans) >= MAX_PLANS:
            raise ConsoleError("PLAN_LIMIT")
        before = await self.gateway.read(resource)
        if expected_revision is not None and revision(before) != expected_revision:
            raise ConsoleError("CONCURRENT_CHANGE")
        after = copy.deepcopy(before)
        models = await self.models()
        features = (
            await self.native_search_features()
            if resource.get("kind") == "profile"
            else None
        )
        paths: set[str] = set()
        for change in changes:
            if not isinstance(change, Mapping) or not isinstance(change.get("path"), str):
                raise ConsoleError("INVALID_CHANGE")
            path = change["path"]
            if path in paths:
                raise ConsoleError("INVALID_CHANGE")
            paths.add(path)
            value = change.get("value")
            if resource.get("kind") == "provider":
                if path != "custom_extra_body.reasoning_effort":
                    raise ConsoleError("FIELD_NOT_WRITABLE")
                efforts_method = getattr(self.gateway, "efforts", None)
                capability = await efforts_method(resource.get("id")) if efforts_method else None
                if not isinstance(capability, list) or value not in capability:
                    raise ConsoleError("EFFORT_UNVERIFIED_OR_UNSUPPORTED")
                registry.set_path(after, path, value)
                continue
            field = next(
                (
                    item
                    for item in registry.field_table(
                        resource.get("kind") or "", resource.get("id")
                    )
                    if item["path"] == path
                ),
                None,
            )
            if field is None or (
                not isinstance(value, str)
                and not (field.get("type") == "secret-list" and isinstance(value, list))
            ):
                raise ConsoleError("FIELD_NOT_WRITABLE")
            if field.get("capability") == "setting":
                field_type = field.get("type")
                if field_type == "bool":
                    if value not in ("true", "false"):
                        raise ConsoleError("VALUE_NOT_ALLOWED")
                    registry.set_path(after, path, value == "true")
                    continue
                if field_type == "secret-list":
                    keys = parse_secret_list(value)
                    if keys is None:
                        raise ConsoleError("SECRET_VALUE_INVALID")
                    registry.set_path(after, path, keys)
                    continue
                if field_type == "secret-string":
                    secret = str(value if value is not None else "").strip()
                    if len(secret) > MAX_SECRET_LENGTH:
                        raise ConsoleError("SECRET_VALUE_INVALID")
                    registry.set_path(after, path, secret)
                    continue
                if field_type == "model":
                    if value == "":
                        registry.set_path(after, path, "")
                        continue
                    model = next((item for item in models if item.get("id") == value), None)
                    if model is None or not model.get("enabled"):
                        raise ConsoleError("MODEL_MISSING_OR_DISABLED")
                    if "llm" not in (model.get("capabilities") or []):
                        raise ConsoleError("CAPABILITY_UNVERIFIED_OR_MISMATCH")
                    registry.set_path(after, path, value)
                    continue
                options = registry.web_search_provider_options(features)
                option = next(
                    (item for item in options if item.get("value") == value), None
                )
                if option is None:
                    raise ConsoleError("VALUE_NOT_ALLOWED")
                if option.get("disabled"):
                    raise ConsoleError("NATIVE_SEARCH_PLUGIN_UNAVAILABLE")
                registry.set_path(after, path, value)
                continue
            capability = field.get("capability")
            if value:
                model = next((item for item in models if item.get("id") == value), None)
                if model is None or not model.get("enabled"):
                    raise ConsoleError("MODEL_MISSING_OR_DISABLED")
                if capability not in (model.get("capabilities") or []):
                    raise ConsoleError("CAPABILITY_UNVERIFIED_OR_MISMATCH")
                if capability in ("embedding", "rerank"):
                    raise ConsoleError("INDEX_MIGRATION_REQUIRES_SEPARATE_WORKFLOW")
            elif capability in ("embedding", "rerank"):
                raise ConsoleError("INDEX_MIGRATION_REQUIRES_SEPARATE_WORKFLOW")
            registry.set_path(after, path, value)
        raw_changes = copy.deepcopy(changes)
        plan_id = str(uuid.uuid4())
        plan = {
            "id": plan_id,
            "resource": dict(resource),
            "before": before,
            "after": after,
            "revision": revision(before),
            "changes": raw_changes,
            "expires": _now_ms() + PLAN_TTL_MS,
        }
        if not restore:
            self.plans[plan_id] = plan
        return {
            "id": plan_id,
            "resource": plan["resource"],
            "revision": plan["revision"],
            "changes": display_changes(raw_changes),
            "expiresAt": _iso_from_ms(plan["expires"]),
            "impact": _impact_of(resource),
            "warning": PLAN_WARNING,
        }

    # ------------------------------------------------------------------ 应用
    async def _audit(self, event: Mapping[str, Any]) -> None:
        """追加一条审计（不含配置内容与凭据）。"""
        line = json.dumps(
            {**event, "time": iso_now()}, ensure_ascii=False, separators=(",", ":")
        ) + "\n"
        path = self.state_dir / "audit.jsonl"

        def append() -> None:
            """线程里追加写入。"""
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

        await asyncio.to_thread(append)

    async def apply(self, plan_id: str) -> dict[str, Any]:
        """应用一份计划：revision 双校验 → 写前备份 → 写入 → 回读比对 → 审计。

        Args:
            plan_id: 计划 id。

        Returns:
            ``{status,id,backupId,generationVerified,hotLoadVerified}``。

        Raises:
            ConsoleError: ``READ_ONLY`` / ``WRITE_IN_PROGRESS`` / ``PLAN_EXPIRED`` /
                ``CONCURRENT_CHANGE`` / ``VERIFY_FAILED`` 或备份/写入链路的错误码。
        """
        if self.read_only:
            raise ConsoleError("READ_ONLY")
        if self._busy:
            raise ConsoleError("WRITE_IN_PROGRESS")
        plan = self.plans.get(plan_id)
        if not plan or plan["expires"] < _now_ms():
            raise ConsoleError("PLAN_EXPIRED")
        self._busy = True
        backup_id: str | None = None
        write_attempted = False
        try:
            if revision(await self.gateway.read(plan["resource"])) != plan["revision"]:
                raise ConsoleError("CONCURRENT_CHANGE")
            if plan["changes"]:
                revalidated = await self.plan(
                    plan["resource"], plan["changes"], restore=True
                )
                if revalidated:
                    self.plans.pop(revalidated["id"], None)
            created = await backup_module.backup_and_prune(
                self.data_dir,
                self.state_dir,
                "apply",
                keep=self.limits.get("keep", backup_module.DEFAULT_BACKUP_KEEP),
                max_bytes=self.limits.get(
                    "maxBytes",
                    backup_module.DEFAULT_BACKUP_MAX_MB * backup_module.MEBIBYTE,
                ),
            )
            backup_id = created.get("id")
            if revision(await self.gateway.read(plan["resource"])) != plan["revision"]:
                raise ConsoleError("CONCURRENT_CHANGE")
            await self._audit(
                {
                    "event": "apply-start",
                    "id": plan_id,
                    "resource": plan["resource"],
                    "backupId": backup_id,
                }
            )
            write_attempted = True
            await self.gateway.write(plan["resource"], plan["after"])
            actual = await self.gateway.read(plan["resource"])
            if revision(actual) != revision(plan["after"]):
                raise ConsoleError("VERIFY_FAILED")
            await self._audit(
                {
                    "event": "applied",
                    "id": plan_id,
                    "resource": plan["resource"],
                    "backupId": backup_id,
                }
            )
            self.plans.pop(plan_id, None)
            self._cache = None
            return {
                "status": "saved-and-readback-verified",
                "id": plan_id,
                "backupId": backup_short_id(backup_id),
                "generationVerified": False,
                "hotLoadVerified": False,
            }
        except Exception as exc:
            if write_attempted:
                self._cache = None
            try:
                await self._audit(
                    {
                        "event": "apply-failed",
                        "id": plan_id,
                        "resource": plan.get("resource"),
                        "backupId": backup_id,
                        "code": known_error(exc),
                    }
                )
            except Exception:  # noqa: BLE001 - 审计失败不掩盖原始错误
                pass
            self.plans.pop(plan_id, None)
            details: dict[str, Any] = {
                "id": plan_id,
                "outcome": "possibly-written" if write_attempted else "not-written",
            }
            if backup_id:
                details["backupId"] = backup_short_id(backup_id)
            raise ConsoleError(known_error(exc), details=details) from exc
        finally:
            self._busy = False

    # ------------------------------------------------------------------ 备份视图
    async def list_backups(self) -> list[dict[str, Any]]:
        """备份列表（新到旧）。"""
        return await backup_module.list_backups(self.state_dir)

    async def backup_stats(self) -> dict[str, Any]:
        """备份统计。"""
        return await backup_module.backup_stats(self.state_dir)

    async def backup_diff(
        self,
        backup_id: str,
        resource: Mapping[str, Any],
    ) -> dict[str, Any]:
        """对比「当前配置」与「备份内配置」的已审查字段差异。"""
        if (
            not isinstance(resource, Mapping)
            or resource.get("kind") not in registry.RESOURCE_KINDS
            or not isinstance(resource.get("id"), str)
        ):
            raise ConsoleError("INVALID_RESOURCE")
        current = await self.gateway.read(resource)
        stored = await backup_module.read_backup_resource(
            self.state_dir, backup_id, resource
        )
        return {
            "backupId": backup_id,
            "resource": dict(resource),
            "changes": display_changes(
                restore_changes(resource, current, stored.get("config"))
            ),
            "currentRevision": revision(current),
        }

    async def plan_restore(
        self,
        backup_id: str,
        resource: Mapping[str, Any],
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """生成恢复计划（整对象写回，只有已审查字段差异经过校验）。"""
        if (
            not isinstance(resource, Mapping)
            or resource.get("kind") not in registry.RESOURCE_KINDS
            or not isinstance(resource.get("id"), str)
        ):
            raise ConsoleError("INVALID_PLAN")
        self._purge_expired()
        if len(self.plans) >= MAX_PLANS:
            raise ConsoleError("PLAN_LIMIT")
        before = await self.gateway.read(resource)
        if expected_revision is not None and revision(before) != expected_revision:
            raise ConsoleError("CONCURRENT_CHANGE")
        stored = await backup_module.read_backup_resource(
            self.state_dir, backup_id, resource
        )
        after = copy.deepcopy(stored.get("config"))
        changes = restore_changes(resource, before, after)
        plan_id = str(uuid.uuid4())
        for change in changes:
            if change.get("capability") in ("embedding", "rerank"):
                raise ConsoleError("INDEX_MIGRATION_REQUIRES_SEPARATE_WORKFLOW")
        plan = {
            "id": plan_id,
            "resource": copy.deepcopy(dict(resource)),
            "before": before,
            "after": after,
            "revision": revision(before),
            "changes": changes,
            "expires": _now_ms() + PLAN_TTL_MS,
            "mode": "restore",
            "backupId": backup_id,
        }
        self.plans[plan_id] = plan
        return {
            "id": plan_id,
            "resource": plan["resource"],
            "revision": plan["revision"],
            "changes": display_changes(changes),
            "expiresAt": _iso_from_ms(plan["expires"]),
            "impact": _impact_of(resource),
            "warning": RESTORE_WARNING,
            "mode": "restore",
            "backupId": backup_id,
        }
