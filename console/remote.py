"""远程设备编排层：给 ``console/app.py`` 调用的 7 个入口 + 响应白名单。

对应旧 Node 实现 ``server.mjs`` 的远端分支（``localDevice`` / ``findDevice`` / ``remoteHealth`` /
``remoteSnapshot`` / ``remoteAgentPlan`` / ``remoteAgentApply`` / ``remoteAgentBackups``）。

设计要点：

* **无状态、不缓存**：每次调用自建 adapter / SSH 隧道并在 ``finally`` 里关闭。Node 的 5 秒远程
  快照缓存由 app 层决定是否复刻，本层不持有全局可变状态。
* **local 永远走本机**：``find_device('local')`` 返回本机设备；``remote_*`` 拒绝 ``local``/``local``
  kind，本机快照必须由 app 层用本机 Engine 生成（契约 §4）。
* **只读强制**：http/ssh 快照恒 ``writeEnabled=False``；agent 只在「设备可写 + agent 自身可写」
  时放开写。
* **白名单**：plan/apply/backups/hotLoad 只输出旧实现审查过的字段，错误 ``details`` 也脱敏
  （绝不带路径/凭据）。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import devices
from .errors import ConsoleError

DEFAULT_BACKUP_LIMITS: dict[str, int] = {"keep": 10, "maxBytes": 2048 * 1024 * 1024}

_PLAN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SHORT_BACKUP_ID = re.compile(r"^[0-9a-f]{8,12}$", re.I)
_FULL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_STRICT_RESOURCE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_PROVIDER_RESOURCE_ID = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_RESOURCE_KINDS = frozenset({"profile", "plugin", "provider"})
_SQLITE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")


# --------------------------------------------------------------------- 注入点（测试可 patch）


def _make_http_adapter(*, base_url: str, api_key: str) -> Any:
    """创建远程 HTTP 适配器（测试可替换）。"""
    from .http_adapter import HttpGatewayAdapter

    return HttpGatewayAdapter(base_url=base_url, api_key=api_key)


def _make_agent_adapter(*, base_url: str, token: str) -> Any:
    """创建远程 agent 适配器（测试可替换）。"""
    from .agent_adapter import AgentAdapter

    return AgentAdapter(base_url=base_url, token=token)


def _open_ssh_tunnel(*, ssh: Mapping[str, Any], base_url: str,
                     data_path: str | None) -> Any:
    """创建 SSH 隧道对象（测试可替换）。"""
    from .ssh_tunnel import SshTunnel

    return SshTunnel(ssh, base_url=base_url, data_path=data_path)


# --------------------------------------------------------------------- 公开视图白名单


def parse_resource(value: Any) -> dict[str, Any] | None:
    """校验 ``{kind,id}`` 资源引用（旧实现 ``parseResource``）。"""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    identity = value.get("id")
    if kind not in _RESOURCE_KINDS or not isinstance(identity, str) or not identity:
        return None
    if kind == "provider":
        if len(identity) > 200 or not _PROVIDER_RESOURCE_ID.match(identity):
            return None
        if ".." in identity.split("/"):
            return None
        return {"kind": kind, "id": identity}
    if len(identity) > 128 or not _STRICT_RESOURCE_ID.match(identity):
        return None
    return {"kind": kind, "id": identity}


def public_changes(changes: Any) -> list[dict[str, Any]]:
    """只保留契约字段 ``{path,before,value,capability}``。"""
    if not isinstance(changes, list):
        return []
    result: list[dict[str, Any]] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        result.append({
            "path": item.get("path") if isinstance(item.get("path"), str) else None,
            "before": item["before"] if "before" in item else None,
            "value": item["value"] if "value" in item else None,
            "capability": (item.get("capability")
                           if isinstance(item.get("capability"), str) else None),
        })
    return result


def public_plan(plan: Any) -> dict[str, Any]:
    """远程 agent plan 的审查后视图（旧实现 ``publicPlan``）。"""
    view: dict[str, Any] = {}
    if not isinstance(plan, dict):
        return view
    if isinstance(plan.get("id"), str) and _PLAN_ID.match(plan["id"]):
        view["id"] = plan["id"]
    resource = parse_resource(plan.get("resource"))
    if resource is not None:
        view["resource"] = resource
    revision = plan.get("revision")
    if isinstance(revision, str) and re.match(r"^[A-Za-z0-9._:-]{1,128}$", revision):
        view["revision"] = revision
    view["changes"] = public_changes(plan.get("changes"))
    expires_at = plan.get("expiresAt")
    if isinstance(expires_at, str) and len(expires_at) <= 40:
        view["expiresAt"] = expires_at
    impact = plan.get("impact")
    if isinstance(impact, str) and 0 < len(impact) <= 200:
        view["impact"] = impact
    warning = plan.get("warning")
    if isinstance(warning, str) and 0 < len(warning) <= 500:
        view["warning"] = warning
    return view


def public_backup(item: Any) -> dict[str, Any] | None:
    """备份条目的契约字段白名单（旧实现 ``publicBackup``）。"""
    if not isinstance(item, dict):
        return None
    identity = item.get("id")
    if not isinstance(identity, str):
        return None
    base = re.split(r"[\\/]+", identity)
    base = next((part for part in reversed(base) if part), "")
    if not base or not (_FULL_UUID.match(base) or _SHORT_BACKUP_ID.match(base)):
        return None
    entry: dict[str, Any] = {"id": base}
    created_at = item.get("createdAt")
    if (isinstance(created_at, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", created_at)
            and len(created_at) <= 40):
        entry["createdAt"] = created_at
    byte_count = item.get("bytes")
    if (isinstance(byte_count, (int, float)) and not isinstance(byte_count, bool)
            and math.isfinite(byte_count) and byte_count >= 0):
        entry["bytes"] = int(byte_count)
    files = item.get("files")
    if isinstance(files, int) and not isinstance(files, bool) and files >= 0:
        entry["files"] = files
    sqlite = item.get("sqlite")
    if isinstance(sqlite, str) and _SQLITE_NAME.match(sqlite):
        entry["sqlite"] = sqlite
    return entry


def safe_apply_result(result: Any) -> Any:
    """apply 结果里的 backupId 只保留 UUID 目录名，非 UUID 直接删除该字段。"""
    if not isinstance(result, dict) or not isinstance(result.get("backupId"), str):
        return result
    parts = [part for part in re.split(r"[\\/]+", result["backupId"]) if part]
    base = parts[-1] if parts else ""
    if _FULL_UUID.match(base):
        return {**result, "backupId": base}
    return {key: value for key, value in result.items() if key != "backupId"}


def redact_evidence_text(value: Any, limit: int) -> str:
    """脱敏日志证据里的本机路径与控制字符（旧实现 ``redactEvidenceText``）。"""
    text = str(value)
    text = re.sub(r"[\u0000-\u001f\u007f]", " ", text)
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s\"'<>]*", "[path]", text)
    text = re.sub(r"\\\\[^\s\"'<>]*", "[path]", text)
    text = re.sub(r'(^|[\s"\'=(:,])/[^\s"\'<>]*', r"\1[path]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def public_hot_load(value: Any) -> dict[str, Any]:
    """热重载证据的公开视图：只有布尔 + 脱敏 note/marker。"""
    if not isinstance(value, dict):
        return {"verified": False, "checked": False}
    evidence: dict[str, Any] = {"verified": value.get("verified") is True,
                                "checked": value.get("checked") is True}
    note = value.get("note")
    if isinstance(note, str) and note:
        evidence["note"] = redact_evidence_text(note, 300)
    marker = value.get("marker")
    if isinstance(marker, str) and marker:
        evidence["marker"] = redact_evidence_text(marker, 200)
    return evidence


def sanitize_error_details(details: Any) -> dict[str, Any]:
    """错误详情白名单（仅 id/backupId/outcome/code）。"""
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


def _public_health(result: Any) -> dict[str, Any]:
    """健康检查公开视图（旧实现 ``publicHealth``）：只保留 reachable/latencyMs/error。"""
    health: dict[str, Any] = {"reachable": isinstance(result, dict)
                              and result.get("reachable") is True}
    if isinstance(result, dict):
        latency = result.get("latencyMs")
        if (isinstance(latency, (int, float)) and not isinstance(latency, bool)
                and math.isfinite(latency) and latency >= 0):
            health["latencyMs"] = int(latency)
        error = result.get("error")
        if isinstance(error, str) and _SAFE_CODE.match(error):
            health["error"] = error
    if not health["reachable"] and "error" not in health:
        health["error"] = "DEVICE_UNREACHABLE"
    return health


# --------------------------------------------------------------------- 设备适配器生命周期


def _device_kind(device: Mapping[str, Any]) -> str:
    """设备类型；缺失按 ``http``（旧实现 ``device.kind??'http'``）。"""
    kind = device.get("kind")
    return kind if kind is not None else "http"


async def _device_secret(registry_dir: Path, device: Mapping[str, Any]) -> str:
    """取设备凭据；缺失抛 ``DEVICE_SECRET_REQUIRED``。"""
    secret = await devices.get_secret(Path(registry_dir), str(device.get("id")))
    if not isinstance(secret, str) or not secret:
        raise ConsoleError("DEVICE_SECRET_REQUIRED", "设备缺少凭据")
    return secret


async def _close_quietly(resource: Any) -> None:
    """尽力关闭 adapter / 隧道，忽略清理异常。"""
    if resource is None:
        return
    closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001
        pass


async def _open_remote(device: Mapping[str, Any], *, registry_dir: Path) -> tuple[Any, Any]:
    """按设备类型建 adapter（ssh 额外建隧道），返回 ``(adapter, tunnel)``。"""
    kind = _device_kind(device)
    secret = await _device_secret(registry_dir, device)
    if kind == "agent":
        adapter = _make_agent_adapter(base_url=str(device.get("baseUrl") or ""),
                                      token=secret)
        return adapter, None
    if kind == "ssh":
        ssh = device.get("ssh")
        if not isinstance(ssh, dict):
            raise ConsoleError("DEVICE_INVALID", "ssh 设备缺少 ssh 配置")
        tunnel = _open_ssh_tunnel(ssh=ssh, base_url=str(device.get("baseUrl") or ""),
                                  data_path=ssh.get("dataPath"))
        try:
            base_url = await tunnel.start()
        except ConsoleError as error:
            raise error
        except Exception:  # noqa: BLE001
            raise ConsoleError("DEVICE_SSH_FAILED", "SSH 隧道建立失败") from None
        if not isinstance(base_url, str) or not base_url or not callable(getattr(tunnel, "close", None)):
            await _close_quietly(tunnel)
            raise ConsoleError("DEVICE_SSH_FAILED", "SSH 隧道返回格式无效")
        try:
            adapter = _make_http_adapter(base_url=base_url, api_key=secret)
        except BaseException:
            await _close_quietly(tunnel)
            raise
        return adapter, tunnel
    adapter = _make_http_adapter(base_url=str(device.get("baseUrl") or ""), api_key=secret)
    return adapter, None


def _relay_agent_error(error: BaseException) -> ConsoleError:
    """把 agent 错误转成白名单 details 的 ConsoleError。"""
    if isinstance(error, ConsoleError):
        return ConsoleError(error.code, error.message or error.code,
                            sanitize_error_details(error.details), error.status)
    return ConsoleError("UNEXPECTED", str(error))


async def _run_agent(device: Mapping[str, Any], *, registry_dir: Path,
                     operation: Any) -> Any:
    """建 agent adapter → 执行 → 关闭；错误统一透传白名单。"""
    adapter, tunnel = await _open_remote(device, registry_dir=registry_dir)
    try:
        return await operation(adapter)
    except ConsoleError as error:
        raise _relay_agent_error(error) from None
    except Exception as error:  # noqa: BLE001
        raise _relay_agent_error(error) from None
    finally:
        await _close_quietly(adapter)
        await _close_quietly(tunnel)


def local_device(*, read_only: bool = True) -> dict[str, Any]:
    """本机设备视图（``read_only`` 传 app 的只读标志）。"""
    return {"id": "local", "name": "本机", "kind": "local",
            "readOnly": bool(read_only), "hasSecret": False}


async def find_device(device_id: str, *, registry_dir: Path | None = None,
                      read_only: bool = True) -> dict[str, Any] | None:
    """按 id 找设备：``local`` 永远返回本机设备，其它查注册表。"""
    if device_id == "local":
        return local_device(read_only=read_only)
    if registry_dir is None:
        return None
    for device in await devices.list_devices(Path(registry_dir)):
        if device.get("id") == device_id:
            return device
    return None


async def remote_health(device: Mapping[str, Any], *, registry_dir: Path) -> dict[str, Any]:
    """远程设备连通性：``{reachable,latencyMs?,error?}``；local 恒 ``{reachable: true}``。"""
    if device.get("id") == "local" or _device_kind(device) == "local":
        return {"reachable": True}
    adapter, tunnel = await _open_remote(device, registry_dir=registry_dir)
    started = time.monotonic()
    try:
        result = await adapter.health()
        if (isinstance(result, dict) and "reachable" not in result
                and result.get("status") == "ok"):
            result = {"reachable": True,
                      "latencyMs": int((time.monotonic() - started) * 1000)}
        return _public_health(result)
    finally:
        await _close_quietly(adapter)
        await _close_quietly(tunnel)


async def remote_snapshot(device: Mapping[str, Any], *, registry_dir: Path) -> dict[str, Any]:
    """远程快照：强制只读并打上 ``device:{id,name,kind}`` 标签（旧实现 ``remoteSnapshot``）。"""
    kind = _device_kind(device)
    if device.get("id") == "local" or kind == "local":
        raise ConsoleError("DEVICE_INVALID", "本机快照必须由 app 层本机 Engine 生成")
    adapter, tunnel = await _open_remote(device, registry_dir=registry_dir)
    try:
        snapshot = await adapter.snapshot()
        if not isinstance(snapshot, dict):
            raise ConsoleError("DEVICE_INVALID_RESPONSE", "远程快照无效")
        writable = (kind == "agent" and device.get("readOnly") is False
                    and snapshot.get("writeEnabled") is True)
        return {**snapshot, "writeEnabled": writable,
                "device": {"id": device.get("id"), "name": device.get("name"),
                           "kind": kind}}
    finally:
        await _close_quietly(adapter)
        await _close_quietly(tunnel)


def _require_agent_write(device: Mapping[str, Any]) -> None:
    """远程写权限：只有可写 agent 设备支持（旧实现 ``agentWriteDevice``）。"""
    if _device_kind(device) != "agent" or device.get("readOnly") is not False:
        raise ConsoleError("DEVICE_READ_ONLY", "只有可写 agent 设备支持远程写入")


def _require_agent_backups(device: Mapping[str, Any]) -> None:
    """远程备份：只有 agent 设备支持（旧实现 ``agentBackupsDevice``）。"""
    if _device_kind(device) != "agent":
        raise ConsoleError("DEVICE_READ_ONLY", "只有 agent 设备支持远程备份")


async def remote_agent_backups(device: Mapping[str, Any], *,
                               registry_dir: Path) -> dict[str, Any]:
    """列出远程 agent 备份：``{backups,stats,limits}``（旧实现 ``remoteAgentBackups``）。"""
    _require_agent_backups(device)

    async def operation(adapter: Any) -> dict[str, Any]:
        payload = await adapter.backups()
        raw_backups = payload.get("backups") if isinstance(payload, dict) else None
        items = raw_backups if isinstance(raw_backups, list) else []
        backups = [entry for entry in (public_backup(item) for item in items)
                   if entry is not None]
        stats = payload.get("stats") if isinstance(payload, dict) else None
        if isinstance(stats, dict):
            count = stats.get("count")
            byte_count = stats.get("bytes")
            normalized_stats = {
                "count": (count if isinstance(count, int) and not isinstance(count, bool)
                          else len(backups)),
                "bytes": (max(0, int(byte_count))
                          if isinstance(byte_count, (int, float))
                          and not isinstance(byte_count, bool)
                          and math.isfinite(byte_count) else 0),
            }
        else:
            normalized_stats = {"count": len(backups), "bytes": 0}
        remote_limits = payload.get("limits") if isinstance(payload, dict) else None
        remote_limits = remote_limits if isinstance(remote_limits, dict) else {}
        keep = remote_limits.get("keep")
        keep = keep if isinstance(keep, int) and not isinstance(keep, bool) and keep > 0 \
            else DEFAULT_BACKUP_LIMITS["keep"]
        max_bytes = remote_limits.get("maxBytes")
        max_bytes = (max(0, int(max_bytes))
                     if isinstance(max_bytes, (int, float))
                     and not isinstance(max_bytes, bool)
                     and math.isfinite(max_bytes) else DEFAULT_BACKUP_LIMITS["maxBytes"])
        return {"backups": backups, "stats": normalized_stats,
                "limits": {"keep": keep, "maxBytes": max_bytes}}

    return await _run_agent(device, registry_dir=registry_dir, operation=operation)


async def remote_agent_plan(device: Mapping[str, Any], body: dict[str, Any], *,
                            registry_dir: Path) -> dict[str, Any]:
    """在可写 agent 上生成计划并白名单化（旧实现 ``remoteAgentPlan``）。"""
    _require_agent_write(device)
    payload = dict(body) if isinstance(body, dict) else {}

    async def operation(adapter: Any) -> dict[str, Any]:
        plan = await adapter.plan(payload)
        if not isinstance(plan, dict):
            raise ConsoleError("DEVICE_INVALID_RESPONSE", "Agent 返回的计划无效")
        return public_plan(plan)

    return await _run_agent(device, registry_dir=registry_dir, operation=operation)


async def remote_agent_apply(device: Mapping[str, Any], body: dict[str, Any], *,
                             registry_dir: Path) -> dict[str, Any]:
    """在可写 agent 上应用计划（旧实现 ``remoteAgentApply``）。"""
    _require_agent_write(device)
    payload = dict(body) if isinstance(body, dict) else {}

    async def operation(adapter: Any) -> dict[str, Any]:
        applied = await adapter.apply(payload)
        if not isinstance(applied, dict):
            raise ConsoleError("DEVICE_INVALID_RESPONSE", "Agent 返回的应用结果无效")
        result = applied.get("result")
        if isinstance(result, dict):
            view = safe_apply_result(result)
        else:
            view = safe_apply_result(applied)
        if "hotLoad" in applied:
            view = dict(view) if isinstance(view, dict) else {}
            view["hotLoad"] = public_hot_load(applied.get("hotLoad"))
        return view if isinstance(view, dict) else {}

    return await _run_agent(device, registry_dir=registry_dir, operation=operation)
