"""console.errors 的单元测试：稳定错误码与响应体收敛。"""

from __future__ import annotations

import unittest

from console.errors import UNEXPECTED, ConsoleError, known_error


class ConsoleErrorTest(unittest.TestCase):
    """ConsoleError 的字段与响应体。"""

    def test_defaults(self) -> None:
        error = ConsoleError("CONCURRENT_CHANGE")
        self.assertEqual(error.code, "CONCURRENT_CHANGE")
        self.assertEqual(error.message, "")
        self.assertEqual(error.details, {})
        self.assertEqual(error.status, 400)
        self.assertEqual(str(error), "CONCURRENT_CHANGE")

    def test_to_body_includes_message_and_details(self) -> None:
        error = ConsoleError(
            "VERIFY_FAILED",
            "回读不一致",
            details={"backupId": "abc", "outcome": "possibly-written"},
            status=409,
        )
        body = error.to_body()
        self.assertEqual(body["error"], "VERIFY_FAILED")
        self.assertEqual(body["message"], "回读不一致")
        self.assertEqual(body["backupId"], "abc")
        self.assertEqual(body["outcome"], "possibly-written")
        self.assertEqual(error.status, 409)

    def test_details_never_override_error_code(self) -> None:
        error = ConsoleError("READ_ONLY_MODE", details={"error": "HIJACK"})
        self.assertEqual(error.to_body()["error"], "READ_ONLY_MODE")

    def test_details_are_copied(self) -> None:
        source = {"id": "x"}
        error = ConsoleError("INVALID_PLAN", details=source)
        source["id"] = "changed"
        self.assertEqual(error.details["id"], "x")


class KnownErrorTest(unittest.TestCase):
    """known_error 的收敛规则。"""

    def test_console_error_keeps_code(self) -> None:
        self.assertEqual(known_error(ConsoleError("PLAN_EXPIRED")), "PLAN_EXPIRED")

    def test_object_with_code_attribute(self) -> None:
        class Custom(Exception):
            code = "DEVICE_UNREACHABLE"

        self.assertEqual(known_error(Custom()), "DEVICE_UNREACHABLE")

    def test_unknown_exception_maps_to_unexpected(self) -> None:
        self.assertEqual(known_error(RuntimeError("boom")), UNEXPECTED)
        self.assertEqual(known_error(ValueError("x")), UNEXPECTED)

    def test_bogus_code_maps_to_unexpected(self) -> None:
        class Bogus(Exception):
            code = "not a code!"

        self.assertEqual(known_error(Bogus()), UNEXPECTED)


if __name__ == "__main__":
    unittest.main()
