"""插件后端编排层（旧实现 ``model-console/src/server.mjs`` 的 Python 等价物）。

这里只做三件事：**参数校验、把请求路由到对应模块、按契约收敛响应/错误码**。
业务规则在 :mod:`console.engine` / :mod:`console.backup` / :mod:`console.remote` 等模块里。

响应约定（契约 §3/§4）：

* 成功：HTTP 200 + 业务 JSON（字段与旧实现一致）；
* 业务错误：抛 :class:`console.errors.ConsoleError`，由 ``main.py`` 转成 HTTP 200 +
  ``{"error": CODE, ...details}``；
* 只读模式（``edit_mode=false``）下，写操作统一抛 ``READ_ONLY_MODE``。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import logger

from . import agent_adapter as agent_adapter_module
from . import backup as backup_module
from . import devices as devices_module
from . import diagnostics
from . import native_search_test as native_search_module
from . import paths
from . import registry
from . import remote as remote_module
from . import search_key_test as search_key_module
from .engine import Engine, iso_now
from .errors import ConsoleError, known_error
from .gateway import Gateway

#: 插件版本（与 metadata.yaml 保持一致）。
PLUGIN_VERSION = "v0.1.1"
#: 备份告警的稳定错误码前缀（诊断 → 备份告警条）。
BACKUP_WARNING_PREFIX = "BACKUP_"
#: 诊断导出条数（旧实现默认 200）。
DIAGNOSTICS_EXPORT_LIMIT = 200
#: 诊断每分钟写入上限（窗口挂在 ConsoleApp 实例上）。
DIAGNOSTICS_LIMIT_PER_MINUTE = 120
#: 原生搜索测试限速：每分钟 10 次。
NATIVE_TEST_LIMIT = 10
#: 内置搜索 Key 测试限速：每分钟 20 次。
KEY_TEST_LIMIT = 20
#: 限速窗口（秒）。
TEST_WINDOW_SECONDS = 60.0
#: 搜索 Key 最大长度。
MAX_KEY_LENGTH = 4096
#: 默认备份上限（与旧实现一致）。
DEFAULT_BACKUP_LIMITS: dict[str, int] = {
    "keep": backup_module.DEFAULT_BACKUP_KEEP,
    "maxBytes": backup_module.DEFAULT_BACKUP_MAX_MB * backup_module.MEBIBYTE,
}
#: 原生搜索「档案里没有保存 Key」时的返回文案（与旧实现逐字一致）。
KEY_MISSING_MESSAGE = "该档案尚未保存 Key，请先填写再测试。"

_PLAN_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_FULL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SHORT_BACKUP_ID = re.compile(r"^[0-9a-f]{8,12}$", re.IGNORECASE)
_REVISION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_STRICT_RESOURCE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_PROVIDER_RESOURCE_ID = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_SQLITE_STATE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")


# ---------------------------------------------------------------------- 只读视图


def public_health(result: Any) -> dict[str, Any]:
    """远程健康结果白名单：只允许 ``reachable`` / ``latencyMs`` / ``error``。"""
    source = result if isinstance(result, Mapping) else {}
    health: dict[str, Any] = {"reachable": source.get("reachable") is True}
    latency = source.get("latencyMs")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0:
        health["latencyMs"] = int(latency)
    error = source.get("error")
    if isinstance(error, str) and _ERROR_CODE.match(error):
        health["error"] = error
    if not health["reachable"] and "error" not in health:
        health["error"] = "DEVICE_UNREACHABLE"
    return health


def backup_id_of(value: Any) -> str | None:
    """把备份引用收敛成 UUID 目录名；任何路径形态的非 UUID 一律拒绝。"""
    if not isinstance(value, str):
        return None
    parts = [part for part in re.split(r"[\\/]+", value) if part]
    if not parts:
        return None
    base = parts[-1]
    if _FULL_UUID.match(base) or _SHORT_BACKUP_ID.match(base):
        return base
    return None


def parse_resource(value: Any) -> dict[str, str] | None:
    """校验 ``{kind,id}`` 资源引用（provider id 允许 ``/`` 但不允许穿越）。"""
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    identifier = value.get("id")
    if kind not in registry.RESOURCE_KINDS or not isinstance(identifier, str) or not identifier:
        return None
    if kind == "provider":
        if len(identifier) > 200 or not _PROVIDER_RESOURCE_ID.match(identifier):
            return None
        if any(segment == ".." for segment in identifier.split("/")):
            return None
        return {"kind": kind, "id": identifier}
    if len(identifier) > 128 or not _STRICT_RESOURCE_ID.match(identifier):
        return None
    return {"kind": kind, "id": identifier}


def public_backup(item: Any) -> dict[str, Any] | None:
    """备份条目白名单（id 只暴露 UUID 目录名，不含绝对路径）。"""
    if not isinstance(item, Mapping):
        return None
    identifier = backup_id_of(item.get("id"))
    if not identifier:
        return None
    entry: dict[str, Any] = {"id": identifier}
    created_at = item.get("createdAt")
    if isinstance(created_at, str) and _CREATED_AT.match(created_at) and len(created_at) <= 40:
        entry["createdAt"] = created_at
    size = item.get("bytes")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size >= 0:
        entry["bytes"] = int(size)
    files = item.get("files")
    if isinstance(files, int) and not isinstance(files, bool) and files >= 0:
        entry["files"] = files
    sqlite_state = item.get("sqlite")
    if isinstance(sqlite_state, str) and _SQLITE_STATE.match(sqlite_state):
        entry["sqlite"] = sqlite_state
    return entry


def public_changes(values: Any) -> list[dict[str, Any]]:
    """差异/计划变更白名单：只保留 ``path/before/value/capability``。"""
    if not isinstance(values, list):
        return []
    changes: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        capability = item.get("capability")
        changes.append(
            {
                "path": path if isinstance(path, str) else None,
                "before": item.get("before"),
                "value": item.get("value"),
                "capability": capability if isinstance(capability, str) else None,
            }
        )
    return changes


def public_restore_plan(plan: Any) -> dict[str, Any]:
    """恢复计划视图白名单（不暴露引擎内部字段）。"""
    source = plan if isinstance(plan, Mapping) else {}
    view: dict[str, Any] = {"mode": "restore"}
    identifier = source.get("id")
    if isinstance(identifier, str) and _PLAN_ID.match(identifier):
        view["id"] = identifier
    resource = parse_resource(source.get("resource"))
    if resource:
        view["resource"] = resource
    rev = source.get("revision")
    if isinstance(rev, str) and _REVISION.match(rev):
        view["revision"] = rev
    expires = source.get("expiresAt")
    if isinstance(expires, str) and len(expires) <= 40:
        view["expiresAt"] = expires
    impact = source.get("impact")
    if isinstance(impact, str) and 0 < len(impact) <= 200:
        view["impact"] = impact
    warning = source.get("warning")
    if isinstance(warning, str) and 0 < len(warning) <= 500:
        view["warning"] = warning
    return view


def safe_apply_result(result: Any) -> Any:
    """应用结果里的备份引用只保留 UUID 目录名；非 UUID 时删除该字段。"""
    if not isinstance(result, Mapping) or not isinstance(result.get("backupId"), str):
        return result
    view = dict(result)
    parts = [part for part in re.split(r"[\\/]+", str(result["backupId"])) if part]
    base = parts[-1] if parts else ""
    if _FULL_UUID.match(base):
        view["backupId"] = base
        return view
    view.pop("backupId", None)
    return view


def normalize_stats(stats: Any) -> dict[str, int]:
    """备份统计收敛成 ``{count,bytes}``（与旧实现一致）。"""
    source = stats if isinstance(stats, Mapping) else {}
    count = source.get("count")
    size = source.get("bytes")
    return {
        "count": count if isinstance(count, int) and not isinstance(count, bool) else 0,
        "bytes": max(0, int(size))
        if isinstance(size, (int, float)) and not isinstance(size, bool)
        else 0,
    }


def _accepts_keyword(function: Any, name: str) -> bool:
    """判断可调用对象是否接受某个关键字参数（兼容其它成员的可选 kwargs）。"""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    return name in signature.parameters


class ConsoleApp:
    """插件后端编排器（生命周期 + 端点分发）。"""

    def __init__(self, config: Mapping[str, Any], plugin_data_dir: Path) -> None:
        """创建编排器（不触碰网络，``initialize()`` 才做磁盘发现）。

        Args:
            config: 插件配置（``_conf_schema.json`` 定义的键）。
            plugin_data_dir: 插件数据目录（``<plugin_data>/astrbot_plugin_model_console``）。
        """
        self.config: dict[str, Any] = dict(config or {})
        self.plugin_data_dir = Path(plugin_data_dir)
        self.state_dir: Path | None = None
        self.data_dir: Path | None = None
        self.legacy_dir: Path | None = None
        self.legacy_import: dict[str, Any] | None = None
        #: 旧状态导入的持久化记录（``state/legacy-import.json``），跨插件重载保留。
        self.legacy_record: dict[str, Any] | None = None
        self.gateway: Gateway | None = None
        self.engine: Engine | None = None
        self.started_at: str | None = None
        self.read_only = not self._config_bool("edit_mode", True)
        self.devices_enabled = self._config_bool("devices_enabled", True)
        self.search_test_enabled = self._config_bool("search_test_enabled", True)
        self._writable = False
        self._init_error: ConsoleError | None = None
        self._limiter = diagnostics.DiagnosticLimiter(DIAGNOSTICS_LIMIT_PER_MINUTE)
        self._native_test_times: list[float] = []
        self._key_test_times: list[float] = []

    # ------------------------------------------------------------------ 生命周期
    def _config_bool(self, name: str, default: bool) -> bool:
        """读取布尔配置项；类型不对时用默认值。"""
        value = self.config.get(name, default)
        return value if isinstance(value, bool) else default

    def _record(self, level: str, event: str, fields: Mapping[str, Any] | None = None) -> None:
        """记录一条诊断（状态目录未就绪时静默跳过）。"""
        if self.state_dir is None:
            return
        diagnostics.log_diagnostic(
            self.state_dir,
            level,
            event,
            fields,
            limiter=self._limiter,
        )

    async def initialize(self) -> None:
        """初始化：解析/校验状态目录、导入旧状态、发现 AstrBot data 并创建网关与引擎。

        失败不抛出（插件加载不应被阻断），错误记在 ``_init_error``，需要它的端点会重新抛出。
        """
        self.started_at = self.started_at or iso_now()
        try:
            await asyncio.to_thread(self.plugin_data_dir.mkdir, parents=True, exist_ok=True)
        except OSError:
            pass
        default_state = self.plugin_data_dir / "state"
        try:
            self.state_dir = paths.resolve_state_dir(self.config, default=default_state)
            await asyncio.to_thread(paths.ensure_writable_state_dir, self.state_dir)
            self._writable = True
        except ConsoleError as exc:
            self._init_error = exc
            if self.state_dir is None:
                self.state_dir = default_state
            self._record("error", "state-directory-unavailable", {"code": exc.code})
        # 先读历史导入记录：插件重载后「曾经导入过」的事实仍在，页面不能误报「未导入」。
        if self.state_dir is not None:
            self.legacy_record = await paths.read_legacy_import_record(self.state_dir)
        self.legacy_dir = await asyncio.to_thread(paths.legacy_state_dir)
        await self._import_legacy_state(record=True)
        data_dir: Path | None = None
        try:
            data_dir = await asyncio.to_thread(paths.discover_astrbot_data, self.config)
        except ConsoleError as exc:
            self._init_error = self._init_error or exc
            self._record("warn", "astrbot-data-unavailable", {"code": exc.code})
        if data_dir is not None:
            self.data_dir = data_dir
        if data_dir is not None and self.state_dir is not None and self._writable:
            limits = await asyncio.to_thread(backup_module.backup_limits, self.config)
            self.gateway = Gateway(self.config, data_dir)
            self.gateway.set_read_only(self.read_only)
            self.engine = Engine(
                self.gateway,
                self.state_dir,
                data_dir,
                limits,
                self.read_only,
            )
        self._record("info", "initialized")

    async def close(self) -> None:
        """释放网关的 HTTP 客户端。"""
        gateway = self.gateway
        self.gateway = None
        self.engine = None
        if gateway is not None:
            try:
                await gateway.close()
            except Exception:  # noqa: BLE001 - 卸载路径不抛
                logger.exception("[model_console] 关闭网关时出现未预期异常")

    @staticmethod
    def _record_indicates_import(record: Mapping[str, Any] | None) -> bool:
        """判断一条导入记录是否代表「旧状态已经被吸收进目标目录」。

        ``imported`` 粘性标记优先；兼容旧记录时 ``copied>0`` 或 ``skipped>0`` 也算：
        ``skipped>0`` 说明旧目录文件在目标目录已存在（上一版插件导入过但没有记录文件）。
        """
        if not isinstance(record, Mapping):
            return False
        if record.get("imported") is True:
            return True
        for key in ("copied", "skipped"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return True
        return False

    def _legacy_imported(self) -> bool:
        """页面用：历史导入记录或本次导入任一成立即为 ``True``。"""
        if self._record_indicates_import(self.legacy_record):
            return True
        return bool(self.legacy_import and self.legacy_import.get("imported"))

    def _build_legacy_record(
        self,
        legacy: Path,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """构造要持久化的导入记录（合并历史，保留「曾经导入过」）。"""
        previous = self.legacy_record if isinstance(self.legacy_record, Mapping) else {}
        copied = int(result.get("files") or 0)
        skipped = int(result.get("skipped") or 0)
        now = iso_now()
        previously_imported = self._record_indicates_import(previous)
        first_imported_at = previous.get("firstImportedAt")
        first_imported_at = first_imported_at if isinstance(first_imported_at, str) else None
        if copied > 0 and not first_imported_at:
            first_imported_at = now
        # 语义 = 「旧状态已被吸收进目标目录」：copied>0 本次复制了；skipped>0 说明目标目录里
        # 早就有这些文件（可能是没有记录文件的旧版插件导入的）；历史记录粘性保留。
        absorbed = copied > 0 or skipped > 0
        return {
            "at": now,
            "source": str(legacy),
            "target": str(self.state_dir),
            "copied": copied,
            "skipped": skipped,
            "files": copied + skipped,
            "status": "imported" if copied > 0 else "unchanged",
            "imported": bool(absorbed or previously_imported),
            "firstImportedAt": first_imported_at,
        }

    async def _import_legacy_state(self, *, record: bool) -> dict[str, Any] | None:
        """按配置把旧控制台状态一次性复制进状态目录，并把结果持久化。"""
        if self.state_dir is None:
            return None
        legacy = await asyncio.to_thread(paths.legacy_state_dir)
        self.legacy_dir = legacy
        if not self._config_bool("legacy_state_import", True):
            return None
        if legacy is None:
            return None
        try:
            if legacy.resolve() == self.state_dir.resolve():
                return None
        except OSError:
            pass
        try:
            result = await paths.import_legacy_state(legacy, self.state_dir)
        except ConsoleError as exc:
            self._record("warn", "legacy-state-import-failed", {"code": exc.code})
            return None
        self.legacy_import = result
        record_data = self._build_legacy_record(legacy, result)
        try:
            await paths.write_legacy_import_record(self.state_dir, record_data)
        except ConsoleError as exc:
            # 记录写失败不影响本次导入结果，但页面下次启动会失去历史事实。
            self._record("warn", "legacy-state-record-failed", {"code": exc.code})
        self.legacy_record = record_data
        if record and result.get("files"):
            self._record(
                "info",
                "legacy-state-imported",
                {"detail": f"files {result['files']}, skipped {result['skipped']}"},
            )
        return result

    def _require_engine(self) -> Engine:
        """取引擎；未初始化成功时抛出初始化错误码。"""
        if self.engine is None:
            error = self._init_error or ConsoleError("ASTRBOT_DATA_REQUIRED")
            raise ConsoleError(
                error.code,
                error.message,
                details=dict(error.details),
            )
        return self.engine

    def _require_gateway(self) -> Gateway:
        """取网关；未初始化成功时抛出初始化错误码。"""
        if self.gateway is None:
            error = self._init_error or ConsoleError("ASTRBOT_DATA_REQUIRED")
            raise ConsoleError(error.code, error.message, details=dict(error.details))
        return self.gateway

    # ------------------------------------------------------------------ 设备辅助
    async def _find_device(self, device_id: str) -> dict[str, Any] | None:
        """按 id 找设备；远程模块未就绪时视为不存在。"""
        finder = getattr(remote_module, "find_device", None)
        if not callable(finder):
            return None
        kwargs: dict[str, Any] = {}
        if _accepts_keyword(finder, "registry_dir"):
            kwargs["registry_dir"] = self.state_dir
        if _accepts_keyword(finder, "read_only"):
            kwargs["read_only"] = self.read_only
        return await finder(device_id, **kwargs)

    async def _call_remote(self, name: str, *args: Any) -> Any:
        """调用 ``console.remote`` 的某个函数（自动补 ``registry_dir`` 关键字）。"""
        function = getattr(remote_module, name, None)
        if not callable(function):
            raise ConsoleError(
                "DEVICE_STORE_UNAVAILABLE",
                f"远程设备模块缺少 {name}()",
            )
        if _accepts_keyword(function, "registry_dir"):
            return await function(*args, registry_dir=self.state_dir)
        return await function(*args)

    def _local_device(self) -> dict[str, Any]:
        """本机设备视图（字段与旧实现一致）。"""
        factory = getattr(remote_module, "local_device", None)
        if callable(factory):
            try:
                if _accepts_keyword(factory, "read_only"):
                    device = factory(read_only=self.read_only)
                else:
                    device = factory()
                if inspect.isawaitable(device):
                    raise TypeError("local_device 不应是协程")
                if isinstance(device, Mapping):
                    return dict(device)
            except Exception:  # noqa: BLE001 - 远程模块异常时用本地兜底
                pass
        return {
            "id": "local",
            "name": "本机",
            "kind": "local",
            "readOnly": self.read_only,
            "hasSecret": False,
        }

    async def _agent_write_device(self, device_id: str) -> dict[str, Any]:
        """解析一个允许远程写入的设备（只允许可写 agent）。"""
        device = await self._find_device(device_id)
        if not device:
            raise ConsoleError("DEVICE_NOT_FOUND", "设备不存在")
        if device.get("kind") != "agent" or device.get("readOnly") is not False:
            raise ConsoleError(
                "DEVICE_READ_ONLY",
                "只有可写 agent 设备支持远程写入",
            )
        return device

    async def _agent_backups_device(self, device_id: str) -> dict[str, Any]:
        """解析一个允许远程备份的设备（只允许 agent）。"""
        device = await self._find_device(device_id)
        if not device:
            raise ConsoleError("DEVICE_NOT_FOUND", "设备不存在")
        if device.get("kind") != "agent":
            raise ConsoleError("DEVICE_READ_ONLY", "只有 agent 设备支持远程备份")
        return device

    # ------------------------------------------------------------------ 运行态
    async def health(self) -> dict[str, Any]:
        """运行态：``{status,version,dataPath,stateDir,readOnly,startedAt}``。"""
        return {
            "status": "ok",
            "version": PLUGIN_VERSION,
            "dataPath": str(self.data_dir) if self.data_dir is not None else None,
            "stateDir": str(self.state_dir) if self.state_dir is not None else None,
            "readOnly": self.read_only,
            "startedAt": self.started_at,
        }

    async def state(self) -> dict[str, Any]:
        """状态目录、旧状态导入结果与编辑模式。

        ``legacyImported`` 是**跨进程粘性**的：只要历史记录里有过成功导入，或本进程本次
        导入过，就为 ``True``——避免插件重载后第二次导入 copied=0 让页面误报「未导入」。
        """
        return {
            "stateDir": str(self.state_dir) if self.state_dir is not None else None,
            "legacyStateDir": str(self.legacy_dir) if self.legacy_dir is not None else None,
            "legacyImported": self._legacy_imported(),
            "legacyImport": self.legacy_record,
            "legacyStatePresent": await paths.legacy_state_present(self.legacy_dir),
            "editMode": not self.read_only,
            "writable": self._writable,
            "astrbotData": str(self.data_dir) if self.data_dir is not None else None,
        }

    async def migrate_state(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """手动重跑旧状态导入（``imported`` 表示本次是否复制了新文件，向后兼容）。"""
        if self.read_only:
            raise ConsoleError("READ_ONLY_MODE")
        if self.state_dir is None:
            raise ConsoleError("STATE_DIR_NOT_WRITABLE")
        await asyncio.to_thread(paths.ensure_writable_state_dir, self.state_dir)
        self._writable = True
        result = await self._import_legacy_state(record=True)
        response: dict[str, Any] = {
            "imported": bool(result and result.get("imported")),
            "files": int(result.get("files") or 0) if result else 0,
            "skipped": int(result.get("skipped") or 0) if result else 0,
            "legacyStateDir": str(self.legacy_dir) if self.legacy_dir else None,
            "stateDir": str(self.state_dir),
            "legacyImported": self._legacy_imported(),
            "legacyImport": self.legacy_record,
            "legacyStatePresent": await paths.legacy_state_present(self.legacy_dir),
        }
        return response

    # ------------------------------------------------------------------ 快照 / 档位
    async def snapshot(self, device_id: str | None) -> dict[str, Any]:
        """配置快照；``device`` 非空且非 ``local`` 时取远程设备快照。"""
        if device_id and device_id != "local":
            device = await self._find_device(device_id)
            if not device:
                raise ConsoleError("DEVICE_NOT_FOUND")
            return await self._call_remote("remote_snapshot", device)
        engine = self._require_engine()
        snapshot = await engine.snapshot()
        return {**snapshot, "device": {"id": "local", "name": "本机", "kind": "local"}}

    async def capabilities(self, provider_id: str | None) -> dict[str, Any]:
        """模型推理档位：``{levels:[], verified:bool}``。"""
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 512:
            raise ConsoleError("INVALID_PROVIDER_ID")
        gateway = self._require_gateway()
        levels = await gateway.efforts(provider_id)
        return {
            "levels": levels if isinstance(levels, list) else [],
            "verified": isinstance(levels, list),
        }

    # ------------------------------------------------------------------ 备份
    async def _backup_warnings(self) -> list[dict[str, Any]]:
        """把最近的 ``BACKUP_*`` 诊断折成备份告警条。"""
        if self.state_dir is None:
            return []
        events = await diagnostics.read_diagnostics(
            self.state_dir, limit=DIAGNOSTICS_EXPORT_LIMIT
        )
        warnings: list[dict[str, Any]] = []
        for event in events:
            code = event.get("code")
            if not isinstance(code, str) or not code.startswith(BACKUP_WARNING_PREFIX):
                continue
            detail = event.get("detail")
            warnings.append(
                {
                    "code": code,
                    "time": event.get("time"),
                    "message": detail if isinstance(detail, str) else code,
                }
            )
        return list(reversed(warnings[-5:]))

    async def backups(self, device_id: str | None) -> dict[str, Any]:
        """备份列表：本机 ``{backups,stats,limits,warnings}``；agent 设备走远程。"""
        if device_id and device_id != "local":
            device = await self._agent_backups_device(device_id)
            return await self._call_remote("remote_agent_backups", device)
        engine = self._require_engine()
        backups = await engine.list_backups()
        stats = normalize_stats(await engine.backup_stats())
        public = [public_backup(item) for item in backups]
        return {
            "backups": [item for item in public if item is not None],
            "stats": stats,
            "limits": backup_module.backup_limits(self.config),
            "warnings": await self._backup_warnings(),
        }

    async def backup_diff(
        self,
        backup_id: str,
        query: Mapping[str, str],
    ) -> dict[str, Any]:
        """恢复前差异：``{backupId,resource,changes,currentRevision}``。"""
        identifier = backup_id_of(backup_id)
        if not identifier:
            raise ConsoleError("INVALID_BACKUP_ID")
        resource = parse_resource(
            {"kind": (query or {}).get("kind"), "id": (query or {}).get("id")}
        )
        if resource is None:
            raise ConsoleError("INVALID_RESOURCE")
        engine = self._require_engine()
        result = await engine.backup_diff(identifier, resource)
        current_revision = result.get("currentRevision")
        return {
            "backupId": identifier,
            "resource": parse_resource(result.get("resource")) or resource,
            "changes": public_changes(result.get("changes")),
            "currentRevision": current_revision
            if isinstance(current_revision, str)
            else None,
        }

    async def prune_backups(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """清理旧备份：``{removed:[uuid], stats}``。"""
        body = body or {}
        keep = body.get("keep")
        if keep is not None:
            if (
                isinstance(keep, bool)
                or not isinstance(keep, int)
                or keep < 1
                or keep > 100
            ):
                raise ConsoleError("INVALID_KEEP")
        if self.state_dir is None:
            raise ConsoleError("BACKUPS_UNAVAILABLE")
        limits = backup_module.backup_limits(self.config)
        result = await backup_module.prune_backups(
            self.state_dir,
            keep if keep is not None else limits["keep"],
            limits["maxBytes"],
        )
        removed: list[str] = []
        for item in result.get("removed") or []:
            identifier = backup_id_of(item)
            if identifier:
                removed.append(identifier)
        stats = await backup_module.backup_stats(self.state_dir)
        return {"removed": removed, "stats": normalize_stats(stats)}

    async def restore_plan(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """生成恢复计划（只读模式拒绝）。"""
        if self.read_only:
            raise ConsoleError("READ_ONLY_MODE")
        body = body or {}
        identifier = backup_id_of(body.get("backupId"))
        if not identifier:
            raise ConsoleError("INVALID_BACKUP_ID")
        resource = parse_resource(body.get("resource"))
        if resource is None:
            raise ConsoleError("INVALID_RESOURCE")
        expected = body.get("expectedRevision")
        if expected is not None and (
            not isinstance(expected, str) or len(expected) > 128
        ):
            raise ConsoleError("INVALID_REVISION")
        engine = self._require_engine()
        plan = await engine.plan_restore(identifier, resource, expected)
        view = public_restore_plan(plan)
        view["backupId"] = backup_id_of(plan.get("backupId")) or identifier
        view["changes"] = public_changes(plan.get("changes"))
        return view

    # ------------------------------------------------------------------ 计划 / 应用
    async def plans(
        self,
        body: Mapping[str, Any] | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """生成修改计划；远程只支持可写 agent 设备。"""
        if self.read_only:
            raise ConsoleError("READ_ONLY_MODE")
        body = body or {}
        if device_id and device_id != "local":
            device = await self._agent_write_device(device_id)
            return await self._call_remote("remote_agent_plan", device, dict(body))
        engine = self._require_engine()
        return await engine.plan(
            body.get("resource"),
            body.get("changes"),
            restore=False,
            expected_revision=body.get("expectedRevision"),
        )

    async def apply(
        self,
        body: Mapping[str, Any] | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """应用计划；远程只支持可写 agent 设备。"""
        if self.read_only:
            raise ConsoleError("READ_ONLY_MODE")
        body = body or {}
        if device_id and device_id != "local":
            device = await self._agent_write_device(device_id)
            return await self._call_remote("remote_agent_apply", device, dict(body))
        engine = self._require_engine()
        return safe_apply_result(await engine.apply(body.get("id")))

    # ------------------------------------------------------------------ 设备
    async def devices(self) -> dict[str, Any]:
        """设备列表：本机 + 已登记设备（``devices_enabled=false`` 时只返回本机）。"""
        devices: list[dict[str, Any]] = [self._local_device()]
        if not self.devices_enabled or self.state_dir is None:
            return {"devices": devices}
        lister = getattr(devices_module, "list_devices", None)
        public = getattr(devices_module, "public_device", None)
        if not callable(lister) or not callable(public):
            return {"devices": devices}
        try:
            registered = await lister(self.state_dir)
        except ConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001 - 设备注册表故障不应让页面整体失败
            self._record(
                "warn",
                "device-registry-unavailable",
                {"code": known_error(exc)},
            )
            return {"devices": devices}
        for device in registered if isinstance(registered, list) else []:
            view = public(device)
            if isinstance(view, Mapping):
                devices.append(dict(view))
        return {"devices": devices}

    async def device_health(self, device_id: str) -> dict[str, Any]:
        """设备连通性：本机恒 ``{reachable:true}``。"""
        if not device_id:
            raise ConsoleError("INVALID_DEVICE_ID")
        device = await self._find_device(device_id)
        if not device:
            raise ConsoleError("DEVICE_NOT_FOUND")
        if device.get("kind") == "local":
            return {"reachable": True}
        result = await self._call_remote("remote_health", device)
        return public_health(result)

    async def delete_device(self, device_id: str) -> dict[str, Any]:
        """删除远程设备（本机不可删）。"""
        if not device_id:
            raise ConsoleError("INVALID_DEVICE_ID")
        if device_id == "local":
            raise ConsoleError("LOCAL_DEVICE_READONLY")
        if self.state_dir is None:
            raise ConsoleError("DEVICE_STORE_UNAVAILABLE")
        remover = getattr(devices_module, "remove_device", None)
        if not callable(remover):
            raise ConsoleError("DEVICE_STORE_UNAVAILABLE", "设备存储不可用")
        await remover(self.state_dir, device_id)
        dropper = getattr(remote_module, "drop_device_session", None)
        if callable(dropper):
            try:
                await dropper(device_id)
            except Exception:  # noqa: BLE001 - 会话清理失败不影响删除结果
                pass
        return {"removed": True}

    async def _pair_agent(self, prepared: Mapping[str, Any], pairing_code: str) -> str:
        """用配对码把 agent 设备换成 token。"""
        pair = getattr(agent_adapter_module, "pair_agent", None)
        if not callable(pair):
            raise ConsoleError("DEVICE_ADAPTER_UNAVAILABLE", "缺少 agent-adapter 模块")
        try:
            paired = await pair(prepared, pairing_code)
        except ConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError("DEVICE_AUTH_FAILED", "Agent 配对失败") from exc
        token = paired.get("token") if isinstance(paired, Mapping) else paired
        if not isinstance(token, str) or not token:
            raise ConsoleError("DEVICE_AUTH_FAILED", "Agent 配对未返回有效 token")
        return token

    async def add_device(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """添加远程设备：``{device}``（agent 需要 pairingCode，http/ssh 需要 apiKey）。"""
        if self.read_only:
            raise ConsoleError("READ_ONLY_MODE")
        body = body or {}
        if self.state_dir is None:
            raise ConsoleError("DEVICE_STORE_UNAVAILABLE")
        load_registry = getattr(devices_module, "load_registry", None)
        validate_device = getattr(devices_module, "validate_device", None)
        add_device = getattr(devices_module, "add_device", None)
        public_device = getattr(devices_module, "public_device", None)
        if not all(
            callable(item)
            for item in (load_registry, validate_device, add_device, public_device)
        ):
            raise ConsoleError("DEVICE_STORE_UNAVAILABLE", "设备存储不可用")
        kind = body.get("kind") if isinstance(body.get("kind"), str) else "http"
        read_only = (body.get("readOnly") is not False) if kind == "agent" else True
        prepared_input: dict[str, Any] = {
            "name": body.get("name"),
            "baseUrl": body.get("baseUrl"),
            "kind": kind,
            "readOnly": read_only,
        }
        if kind == "ssh":
            prepared_input["ssh"] = body.get("ssh")
        registry_data = await load_registry(self.state_dir)
        stored = (
            registry_data.get("devices")
            if isinstance(registry_data, Mapping)
            else None
        )
        stored = stored if isinstance(stored, list) else []
        prepared = validate_device(
            prepared_input,
            existing_ids=[item.get("id") for item in stored if isinstance(item, Mapping)],
            existing_devices=stored,
        )
        if kind == "agent":
            pairing_code = body.get("pairingCode")
            if not isinstance(pairing_code, str) or not pairing_code.strip():
                raise ConsoleError("DEVICE_SECRET_REQUIRED", "添加 agent 设备需要 pairingCode")
            credential = await self._pair_agent(prepared, pairing_code.strip())
        else:
            api_key = body.get("apiKey")
            if not isinstance(api_key, str) or not api_key.strip():
                raise ConsoleError(
                    "DEVICE_SECRET_REQUIRED",
                    "添加 HTTP/SSH 设备需要 OpenAPI Key（abk_...）",
                )
            credential = api_key
        device = await add_device(self.state_dir, prepared, credential)
        return {"device": public_device(device)}

    # ------------------------------------------------------------------ 搜索测试
    def _check_rate(self, stamps: list[float], limit: int) -> None:
        """按滑动窗口限速；超限抛 ``TEST_RATE_LIMITED``。"""
        now = time.monotonic()
        stamps[:] = [stamp for stamp in stamps if now - stamp < TEST_WINDOW_SECONDS]
        if len(stamps) >= limit:
            raise ConsoleError("TEST_RATE_LIMITED")
        stamps.append(now)

    async def search_native_test(
        self,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """测试模型原生搜索链路。"""
        if not self.search_test_enabled:
            raise ConsoleError("SEARCH_TEST_DISABLED")
        body = body or {}
        gateway = self._require_gateway()
        profile_id = body.get("profileId") if isinstance(body.get("profileId"), str) else ""
        if not profile_id:
            raise ConsoleError("PROFILE_REQUIRED")
        try:
            config = await gateway.read({"kind": "profile", "id": profile_id})
        except ConsoleError as exc:
            raise ConsoleError("PROFILE_UNAVAILABLE") from exc
        requested = (
            body.get("providerId").strip()
            if isinstance(body.get("providerId"), str)
            else ""
        )
        settings = config.get("provider_settings") if isinstance(config, Mapping) else None
        configured = (
            settings.get("websearch_native_provider_id")
            if isinstance(settings, Mapping)
            else None
        )
        provider_id = requested or (configured if isinstance(configured, str) else "")
        if not provider_id and isinstance(config, Mapping):
            agent_runner = config.get("agent_runner")
            model_config = (
                agent_runner.get("config", {}).get("model")
                if isinstance(agent_runner, Mapping)
                and isinstance(agent_runner.get("config"), Mapping)
                else None
            )
            fallback = (
                model_config.get("provider_id")
                if isinstance(model_config, Mapping)
                else None
            )
            provider_id = fallback if isinstance(fallback, str) else ""
        if not provider_id:
            raise ConsoleError("PROVIDER_REQUIRED")
        self._check_rate(self._native_test_times, NATIVE_TEST_LIMIT)
        tester = getattr(native_search_module, "test_native_search", None)
        if not callable(tester):
            raise ConsoleError("SEARCH_TEST_DISABLED", "原生搜索测试模块未就绪")
        return await tester(config, provider_id)

    async def search_key_test(
        self,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """测试内置搜索提供商 Key（缺 Key 时从指定档案里读）。"""
        if not self.search_test_enabled:
            raise ConsoleError("SEARCH_TEST_DISABLED")
        body = body or {}
        provider = body.get("provider") if isinstance(body.get("provider"), str) else ""
        supported = getattr(search_key_module, "SUPPORTED_PROVIDERS", None)
        supported = supported if isinstance(supported, list) else []
        if not provider or provider not in supported:
            raise ConsoleError("INVALID_PROVIDER")
        key = body.get("key").strip() if isinstance(body.get("key"), str) else ""
        profile_id = body.get("profileId") if isinstance(body.get("profileId"), str) else ""
        if not key:
            if not profile_id:
                raise ConsoleError("KEY_REQUIRED")
            gateway = self._require_gateway()
            try:
                config = await gateway.read({"kind": "profile", "id": profile_id})
            except ConsoleError as exc:
                raise ConsoleError("PROFILE_UNAVAILABLE") from exc
            field = registry.web_search_key_field(provider)
            name = field["path"].split(".")[-1] if field else ""
            settings = (
                config.get("provider_settings") if isinstance(config, Mapping) else None
            )
            raw = settings.get(name) if name and isinstance(settings, Mapping) else None
            if isinstance(raw, list):
                key = str(
                    next((item for item in raw if isinstance(item, str) and item.strip()), "")
                )
            elif isinstance(raw, str):
                key = raw.strip()
            else:
                key = ""
            if not key:
                return {
                    "ok": False,
                    "provider": provider,
                    "label": "",
                    "status": None,
                    "latencyMs": 0,
                    "message": KEY_MISSING_MESSAGE,
                }
        if len(key) > MAX_KEY_LENGTH:
            raise ConsoleError("KEY_TOO_LONG")
        self._check_rate(self._key_test_times, KEY_TEST_LIMIT)
        tester = getattr(search_key_module, "test_search_key", None)
        if not callable(tester):
            raise ConsoleError("SEARCH_TEST_DISABLED", "搜索 Key 测试模块未就绪")
        return await tester(provider, key)

    # ------------------------------------------------------------------ 诊断
    async def diagnostics_export(self) -> dict[str, Any]:
        """脱敏诊断导出：``{events:[]}``。"""
        events = await diagnostics.read_diagnostics(
            self.state_dir,
            limit=DIAGNOSTICS_EXPORT_LIMIT,
        )
        return {"events": events}
