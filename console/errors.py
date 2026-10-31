"""稳定错误码与错误对象。

旧 Node 实现用 ``Error.code`` + ``{error: CODE}`` 响应体表达业务错误。插件里统一用
:class:`ConsoleError` 承载同样的语义：``code`` 必须逐字保留旧错误码，页面按 code 渲染文案。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

from typing import Any

# 常用错误码（与旧实现保持一致；新增错误码必须全大写蛇形）。
ACCESS_DENIED = "ACCESS_DENIED"
INVALID_BODY = "INVALID_BODY"
INVALID_JSON = "INVALID_JSON"
BODY_TOO_LARGE = "BODY_TOO_LARGE"
JSON_REQUIRED = "JSON_REQUIRED"
NOT_FOUND = "NOT_FOUND"
READ_ONLY_MODE = "READ_ONLY_MODE"
ASTRBOT_DATA_REQUIRED = "ASTRBOT_DATA_REQUIRED"
STATE_DIR_NOT_WRITABLE = "STATE_DIR_NOT_WRITABLE"
DEVICE_STORE_UNAVAILABLE = "DEVICE_STORE_UNAVAILABLE"
SEARCH_TEST_DISABLED = "SEARCH_TEST_DISABLED"
UNEXPECTED = "UNEXPECTED"

# 备份/恢复链路（与旧实现 backup.mjs 的码逐字一致）。
BACKUP_PRUNE_FAILED = "BACKUP_PRUNE_FAILED"
BACKUP_SIZE_LIMIT_EXCEEDED = "BACKUP_SIZE_LIMIT_EXCEEDED"
RESTORE_BACKUP_NOT_FOUND = "RESTORE_BACKUP_NOT_FOUND"
RESTORE_BACKUP_INVALID = "RESTORE_BACKUP_INVALID"


class ConsoleError(Exception):
    """带稳定错误码的业务异常。

    Attributes:
        code: 稳定错误码（大写蛇形），页面依赖它渲染文案。
        message: 可展示给用户的简短说明，可为空。
        details: 可安全暴露的附加字段，会并入响应体。
        status: 建议的 HTTP 状态码；本插件业务错误统一回落为 200（见契约 §3）。
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        details: dict[str, Any] | None = None,
        status: int = 400,
    ) -> None:
        super().__init__(message or code)
        self.code = str(code)
        self.message = str(message or "")
        self.details: dict[str, Any] = dict(details or {})
        self.status = int(status)

    def to_body(self) -> dict[str, Any]:
        """转换成响应体：``{"error": code, ...details}``，message 为空时省略。"""
        body: dict[str, Any] = {"error": self.code}
        if self.message:
            body["message"] = self.message
        for key, value in self.details.items():
            body.setdefault(key, value)
        return body


def known_error(exc: BaseException) -> str:
    """把任意异常收敛成稳定错误码。

    旧实现里未预期异常一律返回 ``UNEXPECTED``；这里保持相同语义，避免把 Python
    异常类名泄露给页面。

    Args:
        exc: 捕获到的异常。

    Returns:
        稳定错误码字符串。
    """
    if isinstance(exc, ConsoleError):
        return exc.code
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code and code.replace("_", "").isalnum():
        return code
    return UNEXPECTED
