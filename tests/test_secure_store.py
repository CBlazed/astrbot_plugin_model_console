"""``console.secure_store`` 单元测试：DPAPI 封装、失败降级、密文损坏。

不触碰真实网络；真实 DPAPI 用例只在 Windows 上跑（同一个用户作用域内加解密）。
"""

from __future__ import annotations

import base64
import sys
import unittest

from console import secure_store
from console.secure_store import (
    PROTECTED_PREFIX,
    SECURE_STORE_UNAVAILABLE,
    SecureStoreUnavailable,
    default_runner,
    is_protected,
    protect,
    unprotect,
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


async def fake_runner(operation: str, payload: str) -> str:
    """把 ``FAKE:`` 前缀拼到明文前的可逆假 runner。"""
    raw = base64.b64decode(payload)
    if operation == "protect":
        return base64.b64encode(b"FAKE:" + raw).decode("ascii")
    if operation == "unprotect":
        if not raw.startswith(b"FAKE:"):
            raise ValueError("FAKE_BAD_BLOB")
        return base64.b64encode(raw[5:]).decode("ascii")
    raise ValueError("FAKE_UNKNOWN_OPERATION")


async def failing_runner(_operation: str, _payload: str) -> str:
    """总是失败的 runner。"""
    raise RuntimeError("runner exploded")


def _is_unavailable(error: BaseException) -> bool:
    return isinstance(error, SecureStoreUnavailable) and error.code == SECURE_STORE_UNAVAILABLE


class SecureStoreFakeRunnerTests(unittest.IsolatedAsyncioTestCase):
    """注入 runner 时的加解密与格式校验。"""

    async def test_roundtrip_and_plaintext_not_in_blob(self) -> None:
        value = "abk_secret_value_中文_$"
        calls: list[tuple[str, str]] = []

        async def runner(operation: str, payload: str) -> str:
            calls.append((operation, payload))
            return await fake_runner(operation, payload)

        blob = await protect(value, runner=runner)
        self.assertTrue(blob.startswith(PROTECTED_PREFIX))
        self.assertTrue(is_protected(blob))
        self.assertNotIn(value, blob)
        self.assertEqual(calls[0], ("protect", _b64(value)))
        self.assertEqual(await unprotect(blob, runner=runner), value)
        self.assertEqual(calls[1], ("unprotect", blob[len(PROTECTED_PREFIX):]))

    async def test_explicit_runner_works_on_other_platform(self) -> None:
        blob = await protect("ssh key", runner=fake_runner, platform="linux")
        self.assertEqual(await unprotect(blob, runner=fake_runner, platform="linux"), "ssh key")

    async def test_default_runner_refuses_non_windows(self) -> None:
        with self.assertRaises(SecureStoreUnavailable):
            await protect("x", platform="linux")
        with self.assertRaises(SecureStoreUnavailable):
            await unprotect("dpapi:AAAA", platform="linux")

    async def test_malformed_values_and_blobs_fail_closed(self) -> None:
        for bad in (None, 123, {}, []):
            with self.assertRaises(SecureStoreUnavailable):
                await protect(bad, runner=fake_runner)  # type: ignore[arg-type]
        for bad in (None, "", "plain", "dpapi:", "dpapi:%%%", "DPAPI:AAAA", "dpapi:AAAA\n"):
            with self.assertRaises(SecureStoreUnavailable):
                await unprotect(bad, runner=fake_runner)  # type: ignore[arg-type]
        self.assertTrue(is_protected("dpapi:AAAA"))
        self.assertFalse(is_protected("plaintext"))
        self.assertFalse(is_protected(None))

    async def test_runner_failures_and_malformed_output_never_leak(self) -> None:
        with self.assertRaises(SecureStoreUnavailable):
            await protect("x", runner=failing_runner)
        with self.assertRaises(SecureStoreUnavailable):
            await unprotect("dpapi:AAAA", runner=failing_runner)

        async def empty(_operation: str, _payload: str) -> str:
            return ""

        async def not_base64(_operation: str, _payload: str) -> str:
            return "not base64!"

        async def percent(_operation: str, _payload: str) -> str:
            return "%%%"

        with self.assertRaises(SecureStoreUnavailable):
            await protect("x", runner=empty)
        with self.assertRaises(SecureStoreUnavailable):
            await protect("x", runner=not_base64)
        with self.assertRaises(SecureStoreUnavailable):
            await unprotect("dpapi:AAAA", runner=percent)

    def test_default_runner_is_factory(self) -> None:
        self.assertTrue(callable(default_runner()))
        self.assertTrue(callable(default_runner(platform="win32")))

    def test_unavailable_error_has_stable_code(self) -> None:
        error = SecureStoreUnavailable()
        self.assertEqual(error.code, SECURE_STORE_UNAVAILABLE)
        self.assertEqual(str(error), SECURE_STORE_UNAVAILABLE)


@unittest.skipUnless(sys.platform == "win32", "DPAPI 只有 Windows 可用")
class SecureStoreRealDpapiTests(unittest.IsolatedAsyncioTestCase):
    """真实 DPAPI（CurrentUser）加解密与篡改拒绝。"""

    async def test_real_roundtrip_and_tamper_rejected(self) -> None:
        value = 'abk_$env:PATH; "quote" \'single\' `backtick`\n中文密钥'
        blob = await protect(value)
        self.assertTrue(blob.startswith(PROTECTED_PREFIX))
        self.assertNotIn(value, blob)
        self.assertNotIn(_b64(value), blob)
        self.assertEqual(await unprotect(blob), value)
        payload = bytearray(base64.b64decode(blob[len(PROTECTED_PREFIX):]))
        payload[-1] ^= 0xFF
        tampered = PROTECTED_PREFIX + base64.b64encode(bytes(payload)).decode("ascii")
        with self.assertRaises(SecureStoreUnavailable):
            await unprotect(tampered)


if __name__ == "__main__":
    unittest.main()
