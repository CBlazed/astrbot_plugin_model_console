"""状态目录与 AstrBot data 目录解析。

等价旧实现 ``model-console/src/config.mjs`` 的路径发现语义，但目标从「独立 Node 进程」改为
「AstrBot 插件进程」：

* 状态目录默认落在插件数据目录 ``<plugin_data>/astrbot_plugin_model_console/state``；
  仍可被 ``MODEL_CONSOLE_STATE`` 环境变量或插件配置 ``state_dir`` 覆盖（覆盖时不静默回退）。
* AstrBot data 目录优先取运行中的 AstrBot 自身（``get_astrbot_data_path()``），
  其次是 ``ASTRBOT_DATA`` 环境变量 / 配置项 ``astrbot_data``，最后按旧实现的候选扫描兜底。
* 旧独立控制台的状态目录 ``%LOCALAPPDATA%\\AstrBotModelConsole\\state`` 只做**一次性复制导入**：
  不覆盖目标已有文件、不删除源目录。

本模块只做本地文件系统发现与迁移，不发网络请求、不读凭据。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from .errors import ASTRBOT_DATA_REQUIRED, STATE_DIR_NOT_WRITABLE, ConsoleError

#: 覆盖状态目录的环境变量名（与旧实现一致）。
STATE_DIR_ENV = "MODEL_CONSOLE_STATE"
#: 覆盖 AstrBot data 目录的环境变量名（与旧实现一致）。
ASTRBOT_DATA_ENV = "ASTRBOT_DATA"
#: 旧独立控制台在 LOCALAPPDATA 下的目录名。
LEGACY_DIR_NAME = "AstrBotModelConsole"
#: 旧状态导入结果在状态目录里的持久化文件名（跨进程重载保留「导入过」的事实）。
LEGACY_IMPORT_RECORD = "legacy-import.json"


def _env(name: str) -> str:
    """读取环境变量，缺失或空白时返回空串。"""
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def resolve_state_dir(
    config: Mapping[str, Any],
    *,
    default: Path | str | None = None,
) -> Path:
    """解析状态目录。

    优先级：``MODEL_CONSOLE_STATE`` 环境变量 > 配置项 ``state_dir`` > ``default``。
    配置项里的相对路径按 ``default`` 的父目录解析（即相对插件数据目录）；都没给时按
    旧实现的便携语义回落到 ``<cwd>/state``。

    Args:
        config: 插件配置映射（可能含 ``state_dir``）。
        default: 调用方提供的默认状态目录（插件里是 ``<plugin_data>/<插件名>/state``）。

    Returns:
        绝对路径（不保证已存在，写入前由 :func:`ensure_writable_state_dir` 校验）。
    """
    raw_env = _env(STATE_DIR_ENV)
    if raw_env:
        return Path(raw_env).expanduser().resolve()

    raw_config = config.get("state_dir") if config else None
    if isinstance(raw_config, str) and raw_config.strip():
        candidate = Path(raw_config.strip()).expanduser()
        if not candidate.is_absolute():
            base = Path(default) if default is not None else Path.cwd()
            candidate = base.parent / candidate
        return candidate.resolve()

    if default is not None:
        return Path(default).expanduser().resolve()
    return (Path.cwd() / "state").resolve()


def legacy_state_dir() -> Path | None:
    """返回旧独立控制台的状态目录（存在才返回，否则 ``None``）。

    Windows 取 ``%LOCALAPPDATA%\\AstrBotModelConsole\\state``；其他平台取
    ``$XDG_STATE_HOME`` / ``~/.local/state`` 下的同名目录，仅作开发环境迁移用途。
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / LEGACY_DIR_NAME / "state"
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
        candidate = base / LEGACY_DIR_NAME / "state"
    try:
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


def _probe_writable(directory: Path) -> str | None:
    """探测目录可写；返回 ``None`` 表示成功，否则返回失败原因。"""
    probe = directory / f".write-probe-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def ensure_writable_state_dir(state_dir: Path) -> None:
    """确认状态目录可写（创建 + 写探针）。

    Args:
        state_dir: 已解析的状态目录。

    Raises:
        ConsoleError: 目录无法创建或不可写，错误码 ``STATE_DIR_NOT_WRITABLE``。
    """
    directory = Path(state_dir)
    failure = _probe_writable(directory)
    if failure is None:
        return
    raise ConsoleError(
        STATE_DIR_NOT_WRITABLE,
        f"STATE_DIR_NOT_WRITABLE: 状态目录不可写：{directory}。"
        "可能被安全软件或目录权限拦截；请改到可写目录，"
        f"或在插件配置里修改 state_dir / 设置 {STATE_DIR_ENV}。（{failure}）",
        details={"failures": [{"dir": str(directory), "message": failure}]},
    )


def _runtime_data_path() -> Path | None:
    """读取运行中 AstrBot 的 data 目录；不在 AstrBot 进程里时返回 ``None``。"""
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 - 可选依赖
            get_astrbot_data_path,
        )
    except Exception:  # noqa: BLE001 - 缺少 AstrBot 运行时是合法场景（单测/独立运行）
        return None
    try:
        value = get_astrbot_data_path()
    except Exception:  # noqa: BLE001
        return None
    return Path(value) if isinstance(value, str) and value else None


def _candidate_data_dirs() -> list[Path]:
    """按旧实现扫描候选 AstrBot data 目录（launcher 实例 + 经典 ~/.astrbot/data）。"""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates: list[Path] = []
    instances_root = home / ".astrbot_launcher" / "instances"
    try:
        entries = list(os.scandir(instances_root))
    except OSError:
        entries = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        candidate = Path(entry.path) / "core" / "data"
        if candidate.is_dir():
            candidates.append(candidate)
    classic = home / ".astrbot" / "data"
    if classic.is_dir():
        candidates.append(classic)
    return candidates


def _validate_data_dir(directory: Path) -> str | None:
    """校验 data 目录：必须是目录且含 ``cmd_config.json``；返回错误说明或 ``None``。"""
    try:
        if not directory.is_dir():
            return f"AstrBot data 目录不存在或不可读：{directory}"
    except OSError:
        return f"AstrBot data 目录不存在或不可读：{directory}"
    if not (directory / "cmd_config.json").is_file():
        return (
            f"{directory} 不是可用的 AstrBot data 目录（缺少 cmd_config.json）。"
            f"请用 {ASTRBOT_DATA_ENV} 或插件配置 astrbot_data 指向正确目录。"
        )
    return None


def discover_astrbot_data(config: Mapping[str, Any]) -> Path:
    """发现 AstrBot data 目录。

    优先级：``ASTRBOT_DATA`` 环境变量 > 配置项 ``astrbot_data`` > 运行中 AstrBot 自身 >
    旧实现的候选扫描（恰好一个才算命中）。

    Args:
        config: 插件配置映射（可能含 ``astrbot_data``）。

    Returns:
        data 目录的绝对路径。

    Raises:
        ConsoleError: 找不到或存在歧义，错误码 ``ASTRBOT_DATA_REQUIRED``。
    """
    raw_env = _env(ASTRBOT_DATA_ENV)
    if raw_env:
        directory = Path(raw_env).expanduser().resolve()
        problem = _validate_data_dir(directory)
        if problem:
            raise ConsoleError(ASTRBOT_DATA_REQUIRED, f"ASTRBOT_DATA_REQUIRED: {problem}")
        return directory

    raw_config = config.get("astrbot_data") if config else None
    if isinstance(raw_config, str) and raw_config.strip():
        directory = Path(raw_config.strip()).expanduser().resolve()
        problem = _validate_data_dir(directory)
        if problem:
            raise ConsoleError(ASTRBOT_DATA_REQUIRED, f"ASTRBOT_DATA_REQUIRED: {problem}")
        return directory

    runtime = _runtime_data_path()
    if runtime is not None and _validate_data_dir(runtime) is None:
        return runtime.resolve()

    candidates = _candidate_data_dirs()
    valid = [path for path in candidates if _validate_data_dir(path) is None]
    if len(valid) == 1:
        return valid[0].resolve()
    if not valid:
        raise ConsoleError(
            ASTRBOT_DATA_REQUIRED,
            "ASTRBOT_DATA_REQUIRED: 未找到 AstrBot 数据目录。已检查运行中的 AstrBot、"
            "%USERPROFILE%\\.astrbot_launcher\\instances\\*\\core\\data 与 "
            "%USERPROFILE%\\.astrbot\\data。请先启动一次 AstrBot，或在插件配置 astrbot_data / "
            f"环境变量 {ASTRBOT_DATA_ENV} 里指定其 data 目录。",
            details={"candidates": 0},
        )
    raise ConsoleError(
        ASTRBOT_DATA_REQUIRED,
        f"ASTRBOT_DATA_REQUIRED: 发现 {len(valid)} 个 AstrBot 数据目录，无法自动选择。"
        "请在插件配置 astrbot_data 或环境变量 ASTRBOT_DATA 里指定其中一个。",
        details={"candidates": len(valid)},
    )


def _import_legacy_sync(legacy: Path, target: Path) -> dict[str, Any]:
    """同步复制旧状态目录：只补缺失文件，不覆盖、不删源。"""
    copied = 0
    skipped = 0
    for root, _dirnames, filenames in os.walk(legacy):
        root_path = Path(root)
        relative_root = root_path.relative_to(legacy)
        for name in filenames:
            source = root_path / name
            destination = target / relative_root / name
            if destination.exists():
                skipped += 1
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            except OSError:
                skipped += 1
                continue
            copied += 1
    return {
        "imported": copied > 0,
        "files": copied,
        "skipped": skipped,
        "source": str(legacy),
        "target": str(target),
    }


async def import_legacy_state(legacy: Path, target: Path) -> dict[str, Any]:
    """把旧状态目录一次性复制导入新状态目录。

    复制粒度是文件：目标已存在的文件一律跳过（不覆盖），源目录保持原样。
    遍历与复制都放在线程里，避免阻塞事件循环。

    Args:
        legacy: 旧状态目录。
        target: 新状态目录。

    Returns:
        ``{"imported": bool, "files": int, "skipped": int, "source": str, "target": str}``；
        ``files`` 是本次新复制的文件数。
    """
    legacy_path = Path(legacy)
    target_path = Path(target)
    try:
        if not legacy_path.is_dir():
            return {
                "imported": False,
                "files": 0,
                "skipped": 0,
                "source": str(legacy_path),
                "target": str(target_path),
            }
        target_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConsoleError(
            STATE_DIR_NOT_WRITABLE,
            f"STATE_DIR_NOT_WRITABLE: 无法准备状态目录：{target_path}（{exc}）",
        ) from exc
    return await asyncio.to_thread(_import_legacy_sync, legacy_path, target_path)


def _legacy_record_path(state_dir: Path) -> Path:
    """旧状态导入记录文件路径。"""
    return Path(state_dir) / LEGACY_IMPORT_RECORD


def _read_legacy_import_record_sync(state_dir: Path) -> dict[str, Any] | None:
    """同步实现：读导入记录；缺失或损坏返回 ``None``。"""
    try:
        raw = json.loads((_legacy_record_path(state_dir)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


async def read_legacy_import_record(state_dir: Path) -> dict[str, Any] | None:
    """读取上次旧状态导入的持久化记录（跨插件重载保留）。

    Args:
        state_dir: 状态目录。

    Returns:
        记录字典 ``{at,source,target,copied,skipped,files,status,imported,firstImportedAt}``；
        没有记录或文件损坏时返回 ``None``。
    """
    return await asyncio.to_thread(_read_legacy_import_record_sync, Path(state_dir))


def _write_legacy_import_record_sync(state_dir: Path, record: Mapping[str, Any]) -> None:
    """同步实现：原子写导入记录（同目录临时文件 + ``os.replace``）。"""
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = _legacy_record_path(directory)
    temporary = directory / f".{LEGACY_IMPORT_RECORD}.{os.getpid()}.tmp"
    payload = json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)


async def write_legacy_import_record(
    state_dir: Path,
    record: Mapping[str, Any],
) -> None:
    """原子持久化旧状态导入记录（原子追加式更新，不会留下半份文件）。

    Args:
        state_dir: 状态目录。
        record: 要写入的记录字典。

    Raises:
        ConsoleError: 状态目录不可写，错误码 ``STATE_DIR_NOT_WRITABLE``。
    """
    try:
        await asyncio.to_thread(_write_legacy_import_record_sync, Path(state_dir), record)
    except OSError as exc:
        raise ConsoleError(
            STATE_DIR_NOT_WRITABLE,
            f"STATE_DIR_NOT_WRITABLE: 无法写入旧状态导入记录：{Path(state_dir)}（{exc}）",
        ) from exc


def _has_files_sync(directory: Path) -> bool:
    """同步实现：目录里是否至少有一个文件。"""
    try:
        for _root, _dirnames, filenames in os.walk(directory):
            if filenames:
                return True
    except OSError:
        return False
    return False


async def legacy_state_present(legacy: Path | None = None) -> bool:
    r"""旧状态目录当前是否存在且非空。

    Args:
        legacy: 旧状态目录；``None`` 时现查 ``%LOCALAPPDATA%\AstrBotModelConsole\state``。

    Returns:
        目录存在且至少有一个文件时为 ``True``。
    """
    target = Path(legacy) if legacy is not None else legacy_state_dir()
    if target is None:
        return False
    try:
        if not target.is_dir():
            return False
    except OSError:
        return False
    return await asyncio.to_thread(_has_files_sync, target)
