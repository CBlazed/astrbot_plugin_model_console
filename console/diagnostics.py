"""追加式脱敏诊断日志（旧实现 ``model-console/src/diagnostics.mjs`` 的 Python 移植）。

每个事件一行 JSON，写到 ``<state>/diagnostics.jsonl``；超过 1 MiB 轮转为
``diagnostics.1.jsonl`` … ``diagnostics.<keep>.jsonl``（1 最新）。事件字段按白名单收敛：
``{time,level,event,code?,path?,status?,device?,detail?}``；字符串一律强力脱敏，
令牌、密钥、请求体、配置值与绝对文件路径都进不了文件。``detail`` 只放短的人类备注。

**任何失败都被吞掉**：诊断不得打断或拖慢主请求路径。写入口另有每分钟限速
（旧实现的限速在 HTTP 层，插件里收在 :class:`DiagnosticLimiter`，窗口挂在 ConsoleApp 实例上）。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: 活动诊断文件名。
DIAGNOSTICS_FILE = "diagnostics.jsonl"
#: 单个活动文件的最大字节数（超过即轮转）。
DIAGNOSTICS_MAX_BYTES = 1024 * 1024
#: 保留的轮转文件份数。
DIAGNOSTICS_KEEP = 3
#: ``detail`` 的最大长度。
DIAGNOSTICS_MAX_DETAIL = 200
#: 默认每分钟最多记录的诊断事件数。
DIAGNOSTICS_RATE_LIMIT = 120
#: 限速窗口（秒）。
DIAGNOSTICS_WINDOW_SECONDS = 60.0

_LEVELS = frozenset({"info", "warn", "error"})
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DEVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>]*")
_UNC_PATH = re.compile(r"\\\\[^\s\"'<>\\]+\\[^\s\"'<>]*")
_POSIX_PATH = re.compile(r"(^|[\s\"'=(:,])\/[^\s\"'<>]*")
_SECRET_PATTERN = re.compile(
    r"\b(token|secret|password|passwd|api[_-]?key|key|authorization|bearer|cookie|jwt)\b\s*[=:]?\s*\S*",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\u0000-\u001f\u007f]")
_PATH_SAFE = re.compile(r"^\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?$")

_write_lock = threading.Lock()


def _iso(moment: datetime) -> str:
    """UTC 时间格式化成与 ``Date#toISOString`` 相同的字符串。"""
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sanitize_path(value: Any) -> str | None:
    """校验并返回安全的本地请求路径；非法返回 ``None``。

    只接受以 ``/`` 开头、无查询串、无反斜杠/上跳、无控制字符的路径，长度上限 256。
    """
    if not isinstance(value, str):
        return None
    path = re.split(r"[?#]", value)[0]
    if not path or len(path) > 256 or not path.startswith("/"):
        return None
    if "\\" in path or ".." in path:
        return None
    if _CONTROL.search(path):
        return None
    if not _PATH_SAFE.match(path):
        return None
    return path


def redact_detail(value: Any) -> str | None:
    """把自由文本压成不能携带凭据或文件系统布局的短备注。"""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value)
    text = _WINDOWS_PATH.sub("[path]", text)
    text = _UNC_PATH.sub("[path]", text)
    text = _POSIX_PATH.sub(r"\1[path]", text)
    text = _SECRET_PATTERN.sub("[redacted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:DIAGNOSTICS_MAX_DETAIL]


def _normalize_time(value: Any, now: datetime) -> str:
    """把事件时间收敛成 ISO 字符串；非法或缺失时用 ``now``。"""
    if isinstance(value, str) and len(value) <= 32 and _ISO_TIME.match(value):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return _iso(datetime.fromisoformat(text))
        except ValueError:
            pass
    return _iso(now)


def normalize_event(
    event_input: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """校验并脱敏一个事件；无法安全记录时返回 ``None``。"""
    if not isinstance(event_input, Mapping):
        return None
    event = event_input.get("event")
    if not isinstance(event, str) or not _EVENT_NAME.match(event):
        return None
    moment = now or datetime.now(timezone.utc)
    level = event_input.get("level")
    record: dict[str, Any] = {
        "time": _normalize_time(event_input.get("time"), moment),
        "level": level if level in _LEVELS else "info",
        "event": event,
    }
    code = event_input.get("code")
    if isinstance(code, str) and _ERROR_CODE.match(code):
        record["code"] = code
    path = sanitize_path(event_input.get("path"))
    if path:
        record["path"] = path
    status = event_input.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        record["status"] = status
    device = event_input.get("device")
    if isinstance(device, str) and _DEVICE_NAME.match(device):
        record["device"] = device
    detail = redact_detail(event_input.get("detail"))
    if detail:
        record["detail"] = detail
    return record


class DiagnosticLimiter:
    """按滑动窗口限制每分钟记录的诊断事件数（窗口挂在调用方实例上）。"""

    def __init__(
        self,
        limit: int = DIAGNOSTICS_RATE_LIMIT,
        window_seconds: float = DIAGNOSTICS_WINDOW_SECONDS,
    ) -> None:
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self._stamps: list[float] = []

    def reset(self) -> None:
        """清空窗口（测试与显式重置用）。"""
        self._stamps.clear()

    def allow(self) -> bool:
        """登记一次事件；窗口内已满则拒绝（返回 ``False``）。"""
        if self.limit < 0:
            return False
        now = time.monotonic()
        cutoff = now - self.window_seconds
        if self._stamps:
            self._stamps = [stamp for stamp in self._stamps if stamp > cutoff]
        if len(self._stamps) >= self.limit:
            return False
        self._stamps.append(now)
        return True


_default_limiters: dict[str, DiagnosticLimiter] = {}


def reset_rate_limits() -> None:
    """清空模块级默认限速窗口（测试用）。"""
    _default_limiters.clear()


def _limiter_for(state_dir: Path) -> DiagnosticLimiter:
    """取该状态目录的默认限速器（不存在则创建）。"""
    key = str(state_dir)
    limiter = _default_limiters.get(key)
    if limiter is None:
        limiter = DiagnosticLimiter()
        _default_limiters[key] = limiter
    return limiter


def _rotate(directory: Path, file_path: Path, incoming: int, max_bytes: int, keep: int) -> None:
    """活动文件即将超限时轮转（``diagnostics.1`` 最新）。"""
    try:
        size = file_path.stat().st_size
    except OSError:
        return
    if size == 0 or size + incoming <= max_bytes:
        return
    for index in range(keep, 0, -1):
        target = directory / f"diagnostics.{index}.jsonl"
        source = file_path if index == 1 else directory / f"diagnostics.{index - 1}.jsonl"
        try:
            target.unlink()
        except OSError:
            pass
        try:
            os.replace(source, target)
        except FileNotFoundError:
            continue


def _write_record(
    state_dir: Path,
    record: Mapping[str, Any],
    *,
    max_bytes: int,
    keep: int,
) -> bool:
    """单行原子追加（同进程串行化，轮转先于写入）。"""
    directory = Path(state_dir)
    file_path = directory / DIAGNOSTICS_FILE
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    payload = line.encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        _rotate(directory, file_path, len(payload), max_bytes, keep)
        with open(file_path, "ab") as handle:
            handle.write(payload)
    return True


def log_diagnostic(
    state_dir: Path | None,
    level: str,
    event: str,
    fields: Mapping[str, Any] | None = None,
    *,
    limiter: DiagnosticLimiter | None = None,
) -> None:
    """追加一条诊断事件；非法事件、限速或写失败都静默丢弃。

    Args:
        state_dir: 状态目录（``None`` 时直接跳过）。
        level: ``info`` / ``warn`` / ``error``（其它值按 ``info`` 记录）。
        event: 事件名（小写字母开头，允许 ``[a-z0-9_.-]``）。
        fields: 附加白名单字段（``code`` / ``path`` / ``status`` / ``device`` / ``detail``）。
        limiter: 限速器；缺省按状态目录取模块级默认限速器。
    """
    try:
        if state_dir is None:
            return
        directory = Path(state_dir)
        record = normalize_event(
            {
                "level": level,
                "event": event,
                **{key: value for key, value in (fields or {}).items() if key != "level" and key != "event"},
            }
        )
        if not record:
            return
        gate = limiter if limiter is not None else _limiter_for(directory)
        if not gate.allow():
            return
        _write_record(
            directory,
            record,
            max_bytes=DIAGNOSTICS_MAX_BYTES,
            keep=DIAGNOSTICS_KEEP,
        )
    except Exception:  # noqa: BLE001 - 诊断永不打断主流程
        return


def _read_diagnostics_sync(state_dir: Path, limit: int) -> list[dict[str, Any]]:
    """同步实现：按年龄顺序读轮转文件与活动文件，逐行重新校验。"""
    directory = Path(state_dir)
    cap = min(max(1, limit if isinstance(limit, int) else 200), 1000)
    names = [f"diagnostics.{index}.jsonl" for index in range(DIAGNOSTICS_KEEP, 0, -1)]
    names.append(DIAGNOSTICS_FILE)
    events: list[dict[str, Any]] = []
    for name in names:
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            record = normalize_event(parsed if isinstance(parsed, Mapping) else None)
            if record:
                events.append(record)
    return events[-cap:]


async def read_diagnostics(state_dir: Path | None, limit: int = 500) -> list[dict[str, Any]]:
    """读取最近的脱敏诊断事件（旧到新）。

    Args:
        state_dir: 状态目录（``None`` 返回空列表）。
        limit: 最多返回条数（1–1000）。

    Returns:
        重新校验过的事件列表。
    """
    if state_dir is None:
        return []
    return await asyncio.to_thread(_read_diagnostics_sync, Path(state_dir), limit)
