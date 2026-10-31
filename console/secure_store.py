"""Windows DPAPI 凭据保护（CurrentUser 作用域）。

旧 Node 实现用 ``powershell.exe`` 调 ``System.Security.Cryptography.ProtectedData``；插件里改为
``ctypes`` 直接调用 ``crypt32.dll`` 的 ``CryptProtectData`` / ``CryptUnprotectData``（契约 §3），
不启动任何子进程、不经过命令行，因此明文永远不会出现在进程列表或临时文件里。

失败语义与旧实现一致：任何不可用（非 Windows、API 调用失败、密文损坏、格式非法）都抛
:class:`SecureStoreUnavailable`（``code = SECURE_STORE_UNAVAILABLE``），由调用方决定降级策略
（``devices.set_secret`` 会降级为明文并留诊断）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ctypes
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any

SECURE_STORE_UNAVAILABLE = "SECURE_STORE_UNAVAILABLE"
PROTECTED_PREFIX = "dpapi:"
CRYPTPROTECT_UI_FORBIDDEN = 0x1

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

#: DPAPI 操作函数签名：``(operation, base64_input) -> base64_output``。
Runner = Callable[[str, str], Awaitable[str]]

_crypt32: Any = None
_kernel32: Any = None


class SecureStoreUnavailable(Exception):
    """DPAPI 不可用或密文无法解析；调用方可降级为明文。"""

    code = SECURE_STORE_UNAVAILABLE

    def __init__(self, message: str = SECURE_STORE_UNAVAILABLE) -> None:
        super().__init__(message or SECURE_STORE_UNAVAILABLE)


def _unavailable() -> SecureStoreUnavailable:
    """构造统一的不可用异常。"""
    return SecureStoreUnavailable()


def is_protected(value: Any) -> bool:
    """判断存储值是否是 ``dpapi:`` 密文（旧实现的 ``isProtected``）。"""
    return isinstance(value, str) and value.startswith(PROTECTED_PREFIX)


if sys.platform == "win32":  # pragma: no branch - 平台分支只在 Windows 上执行

    class _DataBlob(ctypes.Structure):
        """Win32 ``DATA_BLOB``。"""

        _fields_ = [
            ("cbData", ctypes.c_uint32),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def _load_crypt32() -> Any:
        """延迟加载 crypt32/kernel32，保持模块可在非 Windows 上导入。"""
        global _crypt32, _kernel32
        if _crypt32 is None:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_wchar_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptProtectData.restype = ctypes.c_int
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.POINTER(ctypes.c_wchar_p),
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = ctypes.c_int
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            _crypt32, _kernel32 = crypt32, kernel32
        return _crypt32


def _dpapi_call(operation: str, payload_b64: str) -> str:
    """同步执行一次 DPAPI 调用（在线程里跑），返回 base64 结果。

    Args:
        operation: ``protect`` 或 ``unprotect``。
        payload_b64: 输入字节的 base64。

    Returns:
        输出字节的 base64。

    Raises:
        SecureStoreUnavailable: 平台不支持、参数非法或 API 调用失败。
    """
    if not isinstance(payload_b64, str) or not _BASE64_RE.fullmatch(payload_b64 or ""):
        raise _unavailable()
    try:
        raw = base64.b64decode(payload_b64, validate=True)
    except (binascii.Error, ValueError):
        raise _unavailable() from None
    if sys.platform != "win32":  # pragma: no cover - 非 Windows 没有 DPAPI
        raise _unavailable()
    try:
        crypt32 = _load_crypt32()
        buffer = ctypes.create_string_buffer(raw, len(raw))
        data_in = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        data_out = _DataBlob()
        if operation == "protect":
            ok = crypt32.CryptProtectData(
                ctypes.byref(data_in), None, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out),
            )
        elif operation == "unprotect":
            description = ctypes.c_wchar_p()
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(data_in), ctypes.byref(description), None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out),
            )
            if description:
                _kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
        else:
            raise _unavailable()
        if not ok:
            raise _unavailable()
        try:
            output = ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            if data_out.pbData:
                _kernel32.LocalFree(ctypes.cast(data_out.pbData, ctypes.c_void_p))
        return base64.b64encode(output).decode("ascii")
    except SecureStoreUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 任何 ctypes/OS 错误都收敛成不可用
        raise _unavailable() from None


def default_runner(*, platform: str | None = None) -> Runner:
    """返回默认 DPAPI runner；非 Windows 平台的 runner 总是抛不可用。"""
    resolved = sys.platform if platform is None else platform

    async def runner(operation: str, payload_b64: str) -> str:
        if resolved != "win32":
            raise _unavailable()
        if operation not in ("protect", "unprotect"):
            raise _unavailable()
        if not isinstance(payload_b64, str) or not _BASE64_RE.fullmatch(payload_b64 or ""):
            raise _unavailable()
        return await asyncio.to_thread(_dpapi_call, operation, payload_b64)

    return runner


def _resolve_runner(runner: Runner | None, platform: str | None) -> Runner:
    """选用显式 runner（测试注入）或默认 DPAPI runner。"""
    if runner is not None:
        return runner
    return default_runner(platform=platform)


async def protect(plaintext: str, *, runner: Runner | None = None,
                  platform: str | None = None) -> str:
    """把 UTF-8 明文加密成 ``dpapi:<base64>``。

    Args:
        plaintext: 待保护字符串（必须是非空 ``str``；空串与旧实现一样视为不可保护）。
        runner: 测试注入的 runner；默认走 DPAPI。
        platform: 仅测试用，覆盖平台判断。

    Returns:
        ``dpapi:`` 前缀的密文。

    Raises:
        SecureStoreUnavailable: 平台不支持、输入非法或加密失败。
    """
    if not isinstance(plaintext, str):
        raise _unavailable()
    payload = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    resolved = _resolve_runner(runner, platform)
    try:
        output = await resolved("protect", payload)
    except SecureStoreUnavailable:
        raise
    except Exception:  # noqa: BLE001 - runner 自身错误也收敛成不可用
        raise _unavailable() from None
    if not isinstance(output, str) or not _BASE64_RE.fullmatch(output or ""):
        raise _unavailable()
    return PROTECTED_PREFIX + output


async def unprotect(ciphertext: str, *, runner: Runner | None = None,
                    platform: str | None = None) -> str:
    """把 ``dpapi:<base64>`` 密文解密回 UTF-8 明文。

    Args:
        ciphertext: ``dpapi:`` 前缀的密文。
        runner: 测试注入的 runner；默认走 DPAPI。
        platform: 仅测试用，覆盖平台判断。

    Returns:
        解密后的字符串。

    Raises:
        SecureStoreUnavailable: 格式非法、平台不支持或解密失败。
    """
    if not isinstance(ciphertext, str) or not ciphertext.startswith(PROTECTED_PREFIX):
        raise _unavailable()
    payload = ciphertext[len(PROTECTED_PREFIX):]
    if not payload or not _BASE64_RE.fullmatch(payload):
        raise _unavailable()
    resolved = _resolve_runner(runner, platform)
    try:
        output = await resolved("unprotect", payload)
    except SecureStoreUnavailable:
        raise
    except Exception:  # noqa: BLE001
        raise _unavailable() from None
    if not isinstance(output, str) or not _BASE64_RE.fullmatch(output or ""):
        raise _unavailable()
    try:
        raw = base64.b64decode(output, validate=True)
    except (binascii.Error, ValueError):
        raise _unavailable() from None
    return raw.decode("utf-8", errors="replace")
