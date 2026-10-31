"""设备注册表（``devices.json``）与凭据存储（``secrets.json``）。

行为逐字段对齐旧 Node 实现 ``model-console/src/devices.mjs``：

* ``devices.json`` 只放设备元数据，凭据（``apiKey`` / agent token）单独放在 ``secrets.json``；
* 凭据用 :mod:`console.secure_store` 加密成 ``dpapi:<base64>``；DPAPI 不可用时**降级明文**并写一条
  诊断（不阻塞设备功能），与旧实现 ``setSecret`` 的失败降级语义一致；
* 配对码只在添加设备时使用，既不出现在返回值里也不落库；
* ``public_device`` 是显式白名单视图，凭据、``keyPath``、``dataPath`` 永远不会离开进程。

"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import ConsoleError
from .secure_store import (
    SECURE_STORE_UNAVAILABLE,
    SecureStoreUnavailable,
    is_protected,
    protect,
    unprotect,
)

DEVICE_KINDS = ("http", "ssh", "agent")
SECRETS_VERSION = 2
AUTH_FOR_KIND = {"http": "api-key", "ssh": "api-key", "agent": "agent-token"}

_SSH_HOST_RE = re.compile(r"[\s\\/]")
_SSH_USER_SPACE_RE = re.compile(r"\s")


def device_error(code: str, message: str = "") -> ConsoleError:
    """构造带稳定错误码的 :class:`ConsoleError`（旧实现 ``deviceError``）。"""
    return ConsoleError(code, message)


def _now_iso() -> str:
    """当前 UTC 时间，格式与 ``Date.prototype.toISOString`` 一致（毫秒 + Z）。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _devices_path(registry_dir: Path) -> Path:
    """``devices.json`` 的完整路径。"""
    return Path(registry_dir) / "devices.json"


def _secrets_path(registry_dir: Path) -> Path:
    """``secrets.json`` 的完整路径。"""
    return Path(registry_dir) / "secrets.json"


def _read_json(path: Path, fallback: Any) -> Any:
    """同步读 JSON；文件不存在时返回 fallback（其它错误照旧抛出）。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback


def _atomic_write(path: Path, value: Any) -> None:
    """原子写 JSON（临时文件 + ``os.replace``），与旧实现一致保留 2 空格缩进。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, indent=2, ensure_ascii=False))
        os.replace(temp, path)
    finally:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass


def _record_fallback(registry_dir: Path) -> None:
    """DPAPI 不可用降级明文时写一条脱敏诊断；诊断模块不可用时静默跳过。"""
    try:
        from .diagnostics import log_diagnostic
    except Exception:  # noqa: BLE001 - 诊断是可选副作用，不能影响设备功能
        return
    try:
        log_diagnostic(registry_dir, "warn", "secret-plaintext-fallback",
                       {"code": SECURE_STORE_UNAVAILABLE})
    except Exception:  # noqa: BLE001
        return


def _normalize_base_url(value: Any) -> str:
    """校验并规范化 ``baseUrl``；非法抛 ``DEVICE_INVALID``。"""
    try:
        parts = urlsplit(str(value if value is not None else ""))
    except ValueError:
        raise device_error("DEVICE_INVALID", "baseUrl 不是合法 URL") from None
    if not parts.scheme or not parts.netloc:
        raise device_error("DEVICE_INVALID", "baseUrl 不是合法 URL")
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise device_error("DEVICE_INVALID", "baseUrl 只支持 http/https")
    if parts.username is not None or parts.password is not None:
        raise device_error("DEVICE_INVALID", "baseUrl 不能包含账号密码")
    try:
        port = parts.port
    except ValueError:
        raise device_error("DEVICE_INVALID", "baseUrl 不是合法 URL") from None
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise device_error("DEVICE_INVALID", "baseUrl 不是合法 URL")
    if ":" in hostname:  # IPv6 字面量需要重新加方括号
        hostname = f"[{hostname}]"
    default_port = 80 if scheme == "http" else 443
    netloc = hostname if port in (None, default_port) else f"{hostname}:{port}"
    href = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    return href[:-1] if href.endswith("/") else href


def _optional_path(value: Any, label: str) -> str | None:
    """校验可选的本地路径字符串（``ssh.keyPath`` / ``ssh.dataPath``）。"""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 1024 or "\u0000" in value:
        raise device_error("DEVICE_INVALID", f"{label} 不是合法路径字符串")
    return value


def _normalize_ssh(value: Any) -> dict[str, Any]:
    """校验并规范化 ``ssh`` 配置块；host/user 必填，port/keyPath/dataPath 可选。"""
    ssh = value if isinstance(value, dict) else None
    if ssh is None:
        raise device_error("DEVICE_INVALID", "ssh 设备需要 ssh 配置对象")
    host = ssh.get("host").strip() if isinstance(ssh.get("host"), str) else ""
    if not host or len(host) > 255 or _SSH_HOST_RE.search(host):
        raise device_error("DEVICE_INVALID", "ssh.host 必须是 1-255 个字符的主机名或 IP")
    user = ssh.get("user").strip() if isinstance(ssh.get("user"), str) else ""
    if not user or len(user) > 64 or _SSH_USER_SPACE_RE.search(user):
        raise device_error("DEVICE_INVALID", "ssh.user 必须是 1-64 个字符")
    record: dict[str, Any] = {"host": host, "user": user}
    if ssh.get("port") is not None:
        port = ssh.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
            raise device_error("DEVICE_INVALID", "ssh.port 必须是 1-65535 的整数")
        record["port"] = port
    key_path = _optional_path(ssh.get("keyPath"), "ssh.keyPath")
    if key_path is not None:
        record["keyPath"] = key_path
    data_path = _optional_path(ssh.get("dataPath"), "ssh.dataPath")
    if data_path is not None:
        record["dataPath"] = data_path
    return record


def _secret_message(kind: str) -> str:
    """缺少凭据时按设备类型给可读提示（与旧实现逐字一致）。"""
    if kind == "agent":
        return "添加 agent 设备需要配对码换取的设备 token"
    return "添加 HTTP/SSH 设备需要 OpenAPI Key（abk_...）"


def validate_device(data: dict[str, Any], *, existing_ids: list[str] | None = None,
                    existing_devices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """校验并规范化一条设备记录（旧实现 ``validateDevice``）。

    Args:
        data: 原始设备信息（可含 ``id`` / ``createdAt`` / ``pairingCode``）。
        existing_ids: 已存在的设备 id 列表，用于主键去重。
        existing_devices: 已存在的设备记录，用于「同类型 + 同 baseUrl」去重。

    Returns:
        规范化后的设备记录（不含 ``pairingCode``，不含凭据）。

    Raises:
        ConsoleError: ``DEVICE_INVALID`` / ``DEVICE_EXISTS``。
    """
    ids = list(existing_ids or [])
    devices = list(existing_devices or [])
    if not isinstance(data, dict):
        raise device_error("DEVICE_INVALID", "设备信息必须是对象")
    name = data.get("name").strip() if isinstance(data.get("name"), str) else ""
    if not name or len(name) > 80:
        raise device_error("DEVICE_INVALID", "设备名称必须为 1-80 个字符")
    kind = data.get("kind") if data.get("kind") is not None else "http"
    if kind not in DEVICE_KINDS:
        raise device_error("DEVICE_INVALID", f"不支持的设备类型：{kind}")
    base_url = _normalize_base_url(data.get("baseUrl"))
    for existing in devices:
        if not isinstance(existing, dict) or existing.get("kind") != kind:
            continue
        try:
            if _normalize_base_url(existing.get("baseUrl")) == base_url:
                raise device_error("DEVICE_EXISTS", "同类型、同地址的设备已存在")
        except ConsoleError as error:
            if error.code == "DEVICE_EXISTS":
                raise
    device_id = data.get("id") if isinstance(data.get("id"), str) and data.get("id") else str(uuid.uuid4())
    if device_id in ids:
        raise device_error("DEVICE_EXISTS", "设备 ID 已存在")
    record: dict[str, Any] = {
        "id": device_id,
        "name": name,
        "kind": kind,
        "baseUrl": base_url,
        "readOnly": data.get("readOnly") is not False,
        "auth": {"type": AUTH_FOR_KIND[kind]},
        "createdAt": data.get("createdAt") if data.get("createdAt") is not None else _now_iso(),
    }
    if kind == "ssh":
        record["ssh"] = _normalize_ssh(data.get("ssh"))
    return record


async def load_registry(registry_dir: Path) -> dict[str, Any]:
    """读取设备注册表；缺失或结构非法时返回空注册表。"""
    raw = await asyncio.to_thread(_read_json, _devices_path(registry_dir),
                                  {"version": 1, "devices": []})
    if not isinstance(raw, dict) or not isinstance(raw.get("devices"), list):
        return {"version": 1, "devices": []}
    devices = [item for item in raw["devices"]
               if isinstance(item, dict) and isinstance(item.get("id"), str)]
    return {"version": 1, "devices": devices}


async def save_registry(registry_dir: Path, registry: dict[str, Any]) -> None:
    """原子写设备注册表（``version`` 固定为 1）。"""
    devices = registry.get("devices") if isinstance(registry, dict) else []
    await asyncio.to_thread(_atomic_write, _devices_path(registry_dir),
                            {"version": 1, "devices": list(devices or [])})


async def list_devices(registry_dir: Path) -> list[dict[str, Any]]:
    """列出设备（含 ``hasSecret``，但绝不返回凭据本身）。"""
    registry = await load_registry(registry_dir)
    secrets = await _read_secrets(registry_dir)
    result: list[dict[str, Any]] = []
    for device in registry["devices"]:
        stored = secrets["secrets"].get(device["id"])
        result.append({**device, "hasSecret": isinstance(stored, str) and len(stored) > 0})
    return result


def public_device(device: dict[str, Any]) -> dict[str, Any]:
    """按白名单生成设备公开视图（不含凭据、``keyPath``、``dataPath``）。"""
    kind = device.get("kind")
    view: dict[str, Any] = {
        "id": device.get("id"),
        "name": device.get("name"),
        "kind": kind if kind is not None else "http",
        "readOnly": device.get("readOnly") is not False,
        "hasSecret": device.get("hasSecret") is True,
    }
    if isinstance(device.get("baseUrl"), str):
        view["baseUrl"] = device["baseUrl"]
    ssh = device.get("ssh")
    if isinstance(ssh, dict):
        public_ssh: dict[str, Any] = {}
        if isinstance(ssh.get("host"), str):
            public_ssh["host"] = ssh["host"]
        if isinstance(ssh.get("user"), str):
            public_ssh["user"] = ssh["user"]
        if isinstance(ssh.get("port"), int) and not isinstance(ssh.get("port"), bool):
            public_ssh["port"] = ssh["port"]
        if public_ssh:
            view["ssh"] = public_ssh
    if isinstance(device.get("createdAt"), str):
        view["createdAt"] = device["createdAt"]
    return view


async def _read_secrets(registry_dir: Path) -> dict[str, Any]:
    """读取 ``secrets.json``（缺失/非法结构都回落为空表）。"""
    raw = await asyncio.to_thread(_read_json, _secrets_path(registry_dir),
                                  {"version": SECRETS_VERSION, "secrets": {}})
    secrets = raw.get("secrets") if isinstance(raw, dict) else None
    if not isinstance(secrets, dict):
        secrets = {}
    return {"version": SECRETS_VERSION, "secrets": secrets}


async def get_secret(registry_dir: Path, device_id: str, *,
                     runner: Any = None) -> str | None:
    """取回设备的明文凭据；没有凭据返回 None，密文损坏抛 ``SecureStoreUnavailable``。"""
    secrets = await _read_secrets(registry_dir)
    value = secrets["secrets"].get(device_id)
    if not isinstance(value, str) or not value:
        return None
    if is_protected(value):
        return await unprotect(value, runner=runner)
    return value


async def set_secret(registry_dir: Path, device_id: str, value: str, *,
                     runner: Any = None) -> dict[str, Any]:
    """保存设备凭据；DPAPI 不可用时降级明文并返回 ``{protected: False, code}``。"""
    Path(registry_dir).mkdir(parents=True, exist_ok=True)
    secrets = await _read_secrets(registry_dir)
    stored = value
    report: dict[str, Any] = {"protected": True}
    try:
        stored = await protect(value, runner=runner)
    except SecureStoreUnavailable:
        stored = value
        report = {"protected": False, "code": SECURE_STORE_UNAVAILABLE}
        _record_fallback(Path(registry_dir))
    secrets["secrets"][device_id] = stored
    await asyncio.to_thread(_atomic_write, _secrets_path(registry_dir), secrets)
    return report


async def migrate_secrets(registry_dir: Path, *, runner: Any = None) -> dict[str, Any]:
    """把旧版明文凭据迁移为 DPAPI 密文；失败保留明文并逐条上报。"""
    secrets = await _read_secrets(registry_dir)
    migrated: list[str] = []
    failed: list[dict[str, str]] = []
    for device_id, value in list(secrets["secrets"].items()):
        if not isinstance(value, str) or not value or is_protected(value):
            continue
        try:
            secrets["secrets"][device_id] = await protect(value, runner=runner)
            migrated.append(device_id)
        except SecureStoreUnavailable:
            failed.append({"id": device_id, "code": SECURE_STORE_UNAVAILABLE})
        except Exception:  # noqa: BLE001 - 单条失败不影响其它设备
            failed.append({"id": device_id, "code": "OPERATION_FAILED"})
    if migrated:
        await asyncio.to_thread(_atomic_write, _secrets_path(registry_dir), secrets)
    plaintext_remaining = sum(
        1 for value in secrets["secrets"].values()
        if isinstance(value, str) and value and not is_protected(value)
    )
    return {"migrated": migrated, "failed": failed,
            "plaintextRemaining": plaintext_remaining}


async def remove_secret(registry_dir: Path, device_id: str) -> None:
    """删除一条设备凭据（不存在时静默返回）。"""
    secrets = await _read_secrets(registry_dir)
    if device_id not in secrets["secrets"]:
        return
    del secrets["secrets"][device_id]
    await asyncio.to_thread(_atomic_write, _secrets_path(registry_dir), secrets)


async def add_device(registry_dir: Path, prepared: dict[str, Any], credential: str, *,
                     runner: Any = None) -> dict[str, Any]:
    """新增设备：先校验/去重，再写注册表与凭据，返回带 ``hasSecret`` 的记录。

    Raises:
        ConsoleError: ``DEVICE_INVALID`` / ``DEVICE_EXISTS`` / ``DEVICE_SECRET_REQUIRED``。
    """
    registry = await load_registry(registry_dir)
    device = validate_device(
        prepared,
        existing_ids=[item["id"] for item in registry["devices"]],
        existing_devices=registry["devices"],
    )
    if not isinstance(credential, str) or not credential.strip():
        raise device_error("DEVICE_SECRET_REQUIRED", _secret_message(device["kind"]))
    await save_registry(registry_dir,
                        {"devices": [*registry["devices"], device]})
    await set_secret(Path(registry_dir), device["id"], credential.strip(), runner=runner)
    return {**device, "hasSecret": True}


async def update_device(registry_dir: Path, device_id: str, patch: dict[str, Any], *,
                        runner: Any = None) -> dict[str, Any]:
    """更新设备元数据；``patch.apiKey`` 非空时替换凭据，否则保留旧凭据。"""
    registry = await load_registry(registry_dir)
    devices = registry["devices"]
    index = next((position for position, item in enumerate(devices)
                  if item.get("id") == device_id), -1)
    if index < 0:
        raise device_error("DEVICE_NOT_FOUND", "设备不存在")
    current = devices[index]
    merged = {**current, **(patch or {}), "id": device_id}
    others = [item for item in devices if item.get("id") != device_id]
    next_device = validate_device(merged,
                                  existing_ids=[item["id"] for item in others],
                                  existing_devices=others)
    updated = list(devices)
    updated[index] = next_device
    await save_registry(registry_dir, {"devices": updated})
    api_key = (patch or {}).get("apiKey")
    if isinstance(api_key, str) and api_key.strip():
        await set_secret(Path(registry_dir), device_id, api_key.strip(), runner=runner)
    return next_device


async def remove_device(registry_dir: Path, device_id: str) -> None:
    """删除设备及其凭据；设备不存在抛 ``DEVICE_NOT_FOUND``。"""
    registry = await load_registry(registry_dir)
    remaining = [item for item in registry["devices"] if item.get("id") != device_id]
    if len(remaining) == len(registry["devices"]):
        raise device_error("DEVICE_NOT_FOUND", "设备不存在")
    await save_registry(registry_dir, {"devices": remaining})
    await remove_secret(Path(registry_dir), device_id)
