"""写前备份与保留策略（旧实现 ``model-console/src/backup.mjs`` 的 Python 移植）。

一次备份 = 状态目录 ``backups/<uuid>/``：

* ``cmd_config.json`` 与 ``config/*.json`` 原样复制，逐文件与源文件比对（变了就失败）；
* ``data_v4.db`` 用 ``sqlite3.Connection.backup()`` 做在线备份，并 ``PRAGMA quick_check`` 校验；
* ``manifest.json`` 记录每个文件的 sha256、sqlite 状态与创建时间。

**失败一律向上抛错**（写链路据此阻断）。保留策略的失败只降级为诊断告警，不影响备份本身。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .diagnostics import log_diagnostic
from .errors import (
    BACKUP_PRUNE_FAILED,
    BACKUP_SIZE_LIMIT_EXCEEDED,
    RESTORE_BACKUP_INVALID,
    RESTORE_BACKUP_NOT_FOUND,
    ConsoleError,
)

#: 默认保留份数。
DEFAULT_BACKUP_KEEP = 10
#: 默认备份总大小上限（MiB）。
DEFAULT_BACKUP_MAX_MB = 2048
#: 1 MiB 的字节数。
MEBIBYTE = 1024 * 1024
#: 备份目录名（状态目录下）。
BACKUPS_SUBDIR = "backups"

_UUID_DIRECTORY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SAFE_RESOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PROVIDER_ID_MAX_LENGTH = 200


def _iso_now() -> str:
    """当前 UTC 时间，格式与 ``new Date().toISOString()`` 一致（毫秒 + ``Z``）。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _error_code(exc: BaseException) -> str:
    """取异常上的稳定错误码；没有则 ``UNKNOWN``。"""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "UNKNOWN"


def _backups_dir(state_dir: Path) -> Path:
    """状态目录下的备份根目录。"""
    return Path(state_dir) / BACKUPS_SUBDIR


def backup_keep(config: Mapping[str, Any] | None = None) -> int:
    """从插件配置读保留份数（``backup_keep``），非法或缺失回落 10。"""
    raw = (config or {}).get("backup_keep")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_BACKUP_KEEP
    return raw if raw >= 1 else DEFAULT_BACKUP_KEEP


def backup_max_bytes(config: Mapping[str, Any] | None = None) -> int:
    """从插件配置读备份总大小上限（``backup_max_mb``），0 表示不限。

    Args:
        config: 插件配置。

    Returns:
        字节数；``0`` 表示不做大小限制。
    """
    raw = (config or {}).get("backup_max_mb")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_BACKUP_MAX_MB * MEBIBYTE
    if raw == 0:
        return 0
    if raw < 0:
        return DEFAULT_BACKUP_MAX_MB * MEBIBYTE
    return int(raw * MEBIBYTE)


def backup_limits(config: Mapping[str, Any] | None = None) -> dict[str, int]:
    """返回 ``/backups`` 里展示的保留上限 ``{keep,maxBytes}``。"""
    return {"keep": backup_keep(config), "maxBytes": backup_max_bytes(config)}


def _read_text(path: Path) -> str:
    """读 UTF-8 文本并去掉可能的 BOM（AstrBot 配置是 UTF-8 with BOM）。"""
    return Path(path).read_text(encoding="utf-8-sig")


def _directory_bytes(directory: Path) -> int:
    """递归统计目录占用字节（一次 scandir 拿一级全量）。"""
    total = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return total
    for entry in entries:
        try:
            if entry.is_dir():
                total += _directory_bytes(Path(entry.path))
            elif entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _backup_directories(dest: Path) -> list[dict[str, Any]]:
    """列出候选备份目录：UUID 命名且含 ``manifest.json``。"""
    try:
        entries = list(os.scandir(dest))
    except OSError as exc:
        if getattr(exc, "errno", None) is None and not dest.exists():
            return []
        if isinstance(exc, FileNotFoundError):
            return []
        raise
    found: list[dict[str, Any]] = []
    for entry in entries:
        try:
            if not entry.is_dir() or not _UUID_DIRECTORY.match(entry.name):
                continue
            path = Path(entry.path)
            manifest = path / "manifest.json"
            if not manifest.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        found.append({"path": path, "mtime": mtime})
    return found


def _parse_iso(value: str) -> float | None:
    """解析 ISO 时间串为 epoch 秒；解析失败返回 ``None``。"""
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _manifest_info(path: Path, fallback_time: float) -> dict[str, Any] | None:
    """读取单个备份的 manifest 摘要；缺失视为不是备份，损坏标记为 invalid。"""
    corrupt = False
    manifest: Any = None
    try:
        manifest = json.loads(_read_text(path / "manifest.json"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        corrupt = True
    if not isinstance(manifest, dict):
        corrupt = True
        manifest = None
    created_at = manifest.get("createdAt") if isinstance(manifest, dict) else None
    created_ts = _parse_iso(created_at) if isinstance(created_at, str) else None
    if created_ts is None:
        created_ts = fallback_time
        created_at = datetime.fromtimestamp(fallback_time, timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    sqlite_state = manifest.get("sqlite") if isinstance(manifest, dict) else None
    return {
        "createdAt": created_at,
        "createdTs": created_ts,
        "files": len(files) if isinstance(files, list) else None,
        "sqlite": sqlite_state
        if isinstance(sqlite_state, str)
        else ("invalid" if corrupt else "unknown"),
    }


def _backup_entries(dest: Path) -> list[dict[str, Any]]:
    """汇总所有可用备份的条目（含排序用的时间戳）。"""
    found: list[dict[str, Any]] = []
    for entry in _backup_directories(dest):
        info = _manifest_info(entry["path"], entry["mtime"])
        if info is None:
            continue
        found.append(
            {
                "id": entry["path"].name,
                "createdAt": info["createdAt"],
                "createdTs": info["createdTs"],
                "bytes": _directory_bytes(entry["path"]),
                "files": info["files"],
                "sqlite": info["sqlite"],
                "mtime": entry["mtime"],
                "path": str(entry["path"]),
            }
        )
    return found


def _newest_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按创建时间倒序（同时间按目录 mtime 倒序）。"""
    return sorted(
        entries,
        key=lambda item: (item.get("createdTs") or 0.0, item.get("mtime") or 0.0),
        reverse=True,
    )


def _list_backups_sync(state_dir: Path) -> list[dict[str, Any]]:
    """同步实现：所有可用备份，新到旧。"""
    entries = _newest_first(_backup_entries(_backups_dir(state_dir)))
    return [
        {
            "id": item["id"],
            "createdAt": item["createdAt"],
            "bytes": item["bytes"],
            "files": item["files"],
            "sqlite": item["sqlite"],
        }
        for item in entries
    ]


async def list_backups(state_dir: Path) -> list[dict[str, Any]]:
    """列出状态目录下所有可用备份（新到旧）。

    Args:
        state_dir: 状态目录。

    Returns:
        ``[{id, createdAt, bytes, files, sqlite}]``；``files``/``sqlite`` 可能为 ``None``/字符串状态。
    """
    return await asyncio.to_thread(_list_backups_sync, Path(state_dir))


def _backup_stats_sync(state_dir: Path) -> dict[str, Any]:
    """同步实现：备份数量/占用/条目。"""
    entries = _backup_entries(_backups_dir(state_dir))
    ordered = _newest_first(entries)
    return {
        "count": len(entries),
        "bytes": sum(int(item["bytes"]) for item in entries),
        "items": [
            {
                "id": item["id"],
                "bytes": item["bytes"],
                "createdAt": item["createdAt"],
            }
            for item in ordered
        ],
    }


async def backup_stats(state_dir: Path) -> dict[str, Any]:
    """备份统计 ``{count,bytes,items:[{id,bytes,createdAt}]}``。"""
    return await asyncio.to_thread(_backup_stats_sync, Path(state_dir))


def _warn(state_dir: Path, code: str, detail: str) -> None:
    """备份告警只写诊断；告警链路永不打断备份。"""
    try:
        log_diagnostic(
            state_dir,
            "warn",
            "backup-warning",
            {"code": code, "detail": detail},
        )
    except Exception:  # noqa: BLE001 - 告警失败不影响主流程
        pass


def _prune_backups_sync(
    state_dir: Path,
    keep: Any,
    max_bytes: Any,
    protect: str | None = None,
) -> dict[str, Any]:
    """同步实现：按份数清理，再按总大小上限清理。"""
    dest = _backups_dir(state_dir)
    backups = _backup_directories(dest)
    backups.sort(key=lambda item: item["mtime"], reverse=True)
    ordered = backups
    if protect:
        created = next(
            (entry for entry in backups if str(entry["path"]) == str(protect)), None
        )
        if created is not None:
            ordered = [created, *[entry for entry in backups if entry is not created]]
    keep_count = keep if isinstance(keep, int) and not isinstance(keep, bool) and keep >= 1 else DEFAULT_BACKUP_KEEP
    kept = ordered[:keep_count]
    removed: list[str] = []
    for entry in ordered[keep_count:]:
        try:
            shutil.rmtree(entry["path"], ignore_errors=False)
        except OSError:
            continue
        removed.append(str(entry["path"]))
    bytes_total = sum(_directory_bytes(entry["path"]) for entry in kept)

    cap = max_bytes if isinstance(max_bytes, (int, float)) and not isinstance(max_bytes, bool) else 0
    if cap and cap > 0:
        enforced = _enforce_byte_limit(state_dir, int(cap), protect=protect)
        removed.extend(enforced["removed"])
        bytes_total = enforced["bytes"]
    return {"kept": [str(entry["path"]) for entry in kept], "removed": removed, "bytes": bytes_total}


def _enforce_byte_limit(state_dir: Path, max_bytes: int, protect: str | None) -> dict[str, Any]:
    """删除最旧的可删备份直到总量不超上限；最新一份与 ``protect`` 永不删除。"""
    dest = _backups_dir(state_dir)
    entries = _backup_directories(dest)
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    total = sum(_directory_bytes(entry["path"]) for entry in entries)
    if total <= max_bytes:
        return {"kept": [str(entry["path"]) for entry in entries], "removed": [], "bytes": total}
    removed: list[str] = []
    newest = str(entries[0]["path"]) if entries else None
    removable = [
        entry for entry in entries if str(entry["path"]) != protect and str(entry["path"]) != newest
    ]
    for victim in reversed(removable):
        try:
            shutil.rmtree(victim["path"], ignore_errors=False)
        except OSError as exc:
            _warn(state_dir, BACKUP_PRUNE_FAILED, f"{_error_code(exc)} maxBytes={max_bytes}")
            break
        removed.append(str(victim["path"]))
        entries = _backup_directories(dest)
        entries.sort(key=lambda item: item["mtime"], reverse=True)
        total = sum(_directory_bytes(entry["path"]) for entry in entries)
        if total <= max_bytes:
            break
    if removed:
        _warn(state_dir, BACKUP_SIZE_LIMIT_EXCEEDED, f"maxBytes={max_bytes} bytes={total} removed={len(removed)}")
    return {"kept": [str(entry["path"]) for entry in entries], "removed": removed, "bytes": total}


async def prune_backups(
    state_dir: Path,
    keep: Any,
    max_bytes: Any,
    *,
    protect: str | None = None,
) -> dict[str, Any]:
    """按保留份数与总大小上限清理备份。

    Args:
        state_dir: 状态目录。
        keep: 保留份数（非法值按默认 10）。
        max_bytes: 总大小上限字节数；``0``/``None`` 表示不限。
        protect: 单次备份的绝对路径，永不删除（备份刚创建时用）。

    Returns:
        ``{"kept":[路径],"removed":[路径],"bytes":剩余字节}``。
    """
    return await asyncio.to_thread(
        _prune_backups_sync,
        Path(state_dir),
        keep,
        max_bytes,
        protect,
    )


def _valid_provider_id(value: Any) -> bool:
    """AstrBot provider id 允许 ``/``，但禁止路径穿越/盘符/反斜杠/空段。"""
    return (
        isinstance(value, str)
        and 0 < len(value) <= _PROVIDER_ID_MAX_LENGTH
        and ".." not in value
        and "\\" not in value
        and ":" not in value
        and not value.startswith("/")
        and not value.endswith("/")
        and "//" not in value
    )


def _backup_resource_file(resource: Mapping[str, Any]) -> str:
    """把已审查资源映射到备份内的相对文件路径。"""
    if not isinstance(resource, Mapping) or not isinstance(resource.get("kind"), str):
        raise ConsoleError(RESTORE_BACKUP_INVALID)
    kind = resource["kind"]
    identifier = resource.get("id")
    if not isinstance(identifier, str):
        raise ConsoleError(RESTORE_BACKUP_INVALID)
    if kind == "provider":
        if not _valid_provider_id(identifier):
            raise ConsoleError(RESTORE_BACKUP_INVALID)
        return "cmd_config.json"
    if kind not in ("profile", "plugin") or not _SAFE_RESOURCE_ID.match(identifier) or ".." in identifier:
        raise ConsoleError(RESTORE_BACKUP_INVALID)
    if kind == "profile":
        return "cmd_config.json" if identifier == "default" else f"config/abconf_{identifier}.json"
    return f"config/{identifier}_config.json"


async def read_backup_resource(
    state_dir: Path,
    backup_id: str,
    resource: Mapping[str, Any],
) -> dict[str, Any]:
    """读取某备份里某个资源的配置对象。

    Args:
        state_dir: 状态目录。
        backup_id: 备份目录名（UUID）。
        resource: ``{"kind","id"}``。

    Returns:
        ``{"config": 配置对象, "file": 备份内相对文件路径}``；provider 返回单个 provider 配置。

    Raises:
        ConsoleError: ``RESTORE_BACKUP_NOT_FOUND``（备份/文件/provider 不存在）或
            ``RESTORE_BACKUP_INVALID``（JSON 不可读或结构非法）。
    """
    return await asyncio.to_thread(
        _read_backup_resource_sync,
        Path(state_dir),
        backup_id,
        dict(resource) if isinstance(resource, Mapping) else resource,
    )


def _read_backup_resource_sync(
    state_dir: Path,
    backup_id: Any,
    resource: Any,
) -> dict[str, Any]:
    """同步实现：读取备份内单个资源的配置。"""
    if not isinstance(backup_id, str) or not _UUID_DIRECTORY.match(backup_id):
        raise ConsoleError(RESTORE_BACKUP_NOT_FOUND)
    relative_file = _backup_resource_file(resource)
    directory = _backups_dir(state_dir) / backup_id
    if not (directory / "manifest.json").is_file():
        raise ConsoleError(RESTORE_BACKUP_NOT_FOUND)
    try:
        config = json.loads(_read_text(directory / relative_file))
    except FileNotFoundError as exc:
        raise ConsoleError(RESTORE_BACKUP_NOT_FOUND) from exc
    except (OSError, ValueError) as exc:
        raise ConsoleError(RESTORE_BACKUP_INVALID) from exc
    if not isinstance(config, dict):
        raise ConsoleError(RESTORE_BACKUP_INVALID)
    if resource["kind"] == "provider":
        providers = config.get("provider")
        providers = providers if isinstance(providers, list) else []
        for item in providers:
            if isinstance(item, dict) and item.get("id") == resource["id"]:
                return {"config": item, "file": relative_file}
        raise ConsoleError(RESTORE_BACKUP_NOT_FOUND)
    return {"config": config, "file": relative_file}


def _sha256_of(path: Path) -> str:
    """计算文件的 sha256（流式读，避免大文件进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source: Path, target: Path) -> str:
    """复制单个文件并与源比对（内容变了就失败），返回副本的 sha256。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    source_bytes = source.read_bytes()
    copy_bytes = target.read_bytes()
    if source_bytes != copy_bytes:
        raise ConsoleError("BACKUP_CHANGED_DURING_COPY")
    return hashlib.sha256(copy_bytes).hexdigest()


def _sqlite_backup(source: Path, target: Path) -> None:
    """SQLite 在线备份 + ``PRAGMA quick_check`` 校验。"""
    try:
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ConsoleError("BACKUP_SQLITE_INVALID") from exc
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise ConsoleError("BACKUP_SQLITE_INVALID") from exc
    finally:
        src.close()
    try:
        check = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
        try:
            row = check.execute("PRAGMA quick_check").fetchone()
        finally:
            check.close()
    except sqlite3.Error as exc:
        raise ConsoleError("BACKUP_SQLITE_INVALID") from exc
    if not row or row[0] != "ok":
        raise ConsoleError("BACKUP_SQLITE_INVALID")


def _config_file_names(data_dir: Path) -> list[str]:
    """列 ``data/config`` 下要备份的 ``*.json`` 文件名。"""
    config_dir = Path(data_dir) / "config"
    try:
        entries = list(os.scandir(config_dir))
    except OSError:
        return []
    names = [entry.name for entry in entries if entry.is_file() and entry.name.endswith(".json")]
    names.sort()
    return names


def _ensure_backup_sync(data_dir: Path, state_dir: Path, reason: str) -> dict[str, Any]:
    """同步实现：创建一次校验过的备份，然后跑保留策略。"""
    dest = _backups_dir(state_dir)
    backup_id = str(uuid.uuid4())
    directory = dest / backup_id
    directory.mkdir(parents=True, exist_ok=True)

    names = _config_file_names(data_dir)
    files = ["cmd_config.json", *[f"config/{name}" for name in names]]
    manifest: list[dict[str, str]] = []
    for name in files:
        source = Path(data_dir) / name
        target = directory / name
        digest = _copy_verified(source, target)
        manifest.append({"name": name, "sha256": digest})

    _sqlite_backup(Path(data_dir) / "data_v4.db", directory / "data_v4.db")

    created_at = _iso_now()
    (directory / "manifest.json").write_text(
        json.dumps(
            {"files": manifest, "sqlite": "ok", "createdAt": created_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "id": backup_id,
        "path": str(directory),
        "createdAt": created_at,
        "files": len(manifest),
        "sqlite": "ok",
        "reason": reason,
    }


async def ensure_backup(data_dir: Path, state_dir: Path, reason: str) -> dict[str, Any]:
    """创建一次校验过的备份；任何一步失败都向上抛错（写链路据此阻断）。

    Args:
        data_dir: AstrBot data 目录（含 ``cmd_config.json``/``config``/``data_v4.db``）。
        state_dir: 状态目录（备份写到 ``<state>/backups/<uuid>``）。
        reason: 触发原因（``apply`` / ``manual``），只用于审计与诊断。

    Returns:
        ``{"id","path","createdAt","files","sqlite","reason"}``；``id`` 是 UUID 目录名。

    Raises:
        ConsoleError: ``BACKUP_CHANGED_DURING_COPY`` / ``BACKUP_SQLITE_INVALID`` 或文件系统错误。
    """
    # 保留策略参数来自插件配置，但备份模块本身只负责「创建 + 校验」；
    # 清理由 prune_backups 用显式 keep/max_bytes 调用，避免备份函数隐式读环境。
    try:
        return await asyncio.to_thread(
            _ensure_backup_sync, Path(data_dir), Path(state_dir), reason
        )
    except ConsoleError:
        raise
    except OSError as exc:
        # 旧实现里文件系统错误会冒到 HTTP 层并被收敛成 OPERATION_FAILED。
        raise ConsoleError("OPERATION_FAILED") from exc


async def backup_and_prune(
    data_dir: Path,
    state_dir: Path,
    reason: str,
    *,
    keep: Any = DEFAULT_BACKUP_KEEP,
    max_bytes: Any = DEFAULT_BACKUP_MAX_MB * MEBIBYTE,
) -> dict[str, Any]:
    """创建备份后按上限清理（清理失败只降级为告警，不阻断写入）。

    Args:
        data_dir: AstrBot data 目录。
        state_dir: 状态目录。
        reason: 触发原因。
        keep: 保留份数。
        max_bytes: 总大小上限字节数（``0`` 表示不限）。

    Returns:
        与 :func:`ensure_backup` 相同的结果。
    """
    created = await ensure_backup(data_dir, state_dir, reason)
    try:
        await prune_backups(state_dir, keep, max_bytes, protect=created["path"])
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响已完成的备份
        _warn(state_dir, BACKUP_PRUNE_FAILED, _error_code(exc))
    return created
