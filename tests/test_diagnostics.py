"""console.diagnostics 的单元测试：脱敏、白名单、轮转/限速与读取。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from console import diagnostics


class SanitizePathTest(unittest.TestCase):
    """sanitize_path 只放行本地请求路径。"""

    def test_accepts_request_paths(self) -> None:
        self.assertEqual(diagnostics.sanitize_path("/api/v1/state"), "/api/v1/state")
        self.assertEqual(diagnostics.sanitize_path("/health?x=1"), "/health")

    def test_rejects_unsafe(self) -> None:
        self.assertIsNone(diagnostics.sanitize_path("api/v1"))
        self.assertIsNone(diagnostics.sanitize_path("C:\\secret\\file"))
        self.assertIsNone(diagnostics.sanitize_path("/../etc/passwd"))
        self.assertIsNone(diagnostics.sanitize_path("/a b"))
        self.assertIsNone(diagnostics.sanitize_path(None))
        self.assertIsNone(diagnostics.sanitize_path("/" + "a" * 300))


class RedactDetailTest(unittest.TestCase):
    """redact_detail 去掉路径与凭据。"""

    def test_redacts_paths_and_secrets(self) -> None:
        redacted = diagnostics.redact_detail(
            "failed token=abc123 at C:\\Users\\me\\astrbot\\data and /home/me/x.txt"
        )
        self.assertIsNotNone(redacted)
        assert redacted is not None
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("C:\\Users", redacted)
        self.assertNotIn("/home/me", redacted)
        self.assertIn("[redacted]", redacted)
        self.assertIn("[path]", redacted)

    def test_truncates_and_normalizes(self) -> None:
        redacted = diagnostics.redact_detail("a" * 500)
        self.assertIsNotNone(redacted)
        assert redacted is not None
        self.assertEqual(len(redacted), diagnostics.DIAGNOSTICS_MAX_DETAIL)

    def test_non_string(self) -> None:
        self.assertIsNone(diagnostics.redact_detail(123))
        self.assertIsNone(diagnostics.redact_detail("   "))


class NormalizeEventTest(unittest.TestCase):
    """normalize_event 的白名单与取值校验。"""

    def test_invalid_event_names(self) -> None:
        self.assertIsNone(diagnostics.normalize_event(None))
        self.assertIsNone(diagnostics.normalize_event({"event": "Bad-Name"}))
        self.assertIsNone(diagnostics.normalize_event({"event": "9start"}))
        self.assertIsNone(diagnostics.normalize_event({"event": ""}))
        self.assertIsNone(diagnostics.normalize_event(["event"]))

    def test_field_whitelist(self) -> None:
        record = diagnostics.normalize_event(
            {
                "event": "apply-failed",
                "level": "debug",
                "code": "VERIFY_FAILED",
                "path": "/api/v1/apply",
                "status": 500,
                "device": "laptop-1",
                "detail": "readback mismatch",
                "extra": "dropped",
                "token": "should-not-appear",
            }
        )
        assert record is not None
        self.assertEqual(record["event"], "apply-failed")
        self.assertEqual(record["level"], "info")
        self.assertEqual(record["code"], "VERIFY_FAILED")
        self.assertEqual(record["path"], "/api/v1/apply")
        self.assertEqual(record["status"], 500)
        self.assertEqual(record["device"], "laptop-1")
        self.assertEqual(record["detail"], "readback mismatch")
        self.assertNotIn("extra", record)
        self.assertNotIn("token", record)

    def test_invalid_optional_fields_dropped(self) -> None:
        record = diagnostics.normalize_event(
            {
                "event": "request-rejected",
                "code": "lowercase",
                "path": "C:\\x",
                "status": 42,
                "device": "bad device!",
            }
        )
        assert record is not None
        self.assertNotIn("code", record)
        self.assertNotIn("path", record)
        self.assertNotIn("status", record)
        self.assertNotIn("device", record)


class _TempDirCase(unittest.TestCase):
    """带临时状态目录的测试基类。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        diagnostics.reset_rate_limits()


class LogAndReadTest(_TempDirCase):
    """写入与读回。"""

    def test_writes_one_json_line(self) -> None:
        diagnostics.log_diagnostic(
            self.state,
            "warn",
            "backup-warning",
            {"code": "BACKUP_PRUNE_FAILED", "detail": "prune failed"},
        )
        path = self.state / diagnostics.DIAGNOSTICS_FILE
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["level"], "warn")
        self.assertEqual(record["event"], "backup-warning")
        self.assertEqual(record["code"], "BACKUP_PRUNE_FAILED")

    def test_invalid_event_not_written(self) -> None:
        diagnostics.log_diagnostic(self.state, "info", "INVALID EVENT")
        self.assertFalse((self.state / diagnostics.DIAGNOSTICS_FILE).exists())

    def test_none_state_dir_is_noop(self) -> None:
        diagnostics.log_diagnostic(None, "info", "noop")

    def test_rate_limit_drops_overflow(self) -> None:
        limiter = diagnostics.DiagnosticLimiter(limit=2)
        for index in range(5):
            diagnostics.log_diagnostic(
                self.state,
                "info",
                "request-rejected",
                {"detail": f"event {index}"},
                limiter=limiter,
            )
        lines = [
            line
            for line in (self.state / diagnostics.DIAGNOSTICS_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        self.assertEqual(len(lines), 2)

    def test_stale_lines_revalidated_on_read(self) -> None:
        path = self.state / diagnostics.DIAGNOSTICS_FILE
        self.state.mkdir(parents=True, exist_ok=True)
        good = json.dumps({"time": "2026-01-01T00:00:00Z", "level": "info", "event": "seeded"})
        bad = json.dumps({"time": "2026-01-01T00:00:00Z", "level": "info", "event": "BILLBOARD"})
        path.write_text(f"{good}\n{bad}\nnot-json\n", encoding="utf-8")
        events = asyncio.run(diagnostics.read_diagnostics(self.state))
        self.assertEqual([event["event"] for event in events], ["seeded"])

    def test_read_orders_oldest_first_and_limits(self) -> None:
        for index in range(5):
            diagnostics.log_diagnostic(
                self.state, "info", "request-rejected", {"detail": f"event {index}"}
            )
        events = asyncio.run(
            diagnostics.read_diagnostics(self.state, limit=2)
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["detail"], "event 4")

    def test_read_none_state_dir(self) -> None:
        events = asyncio.run(diagnostics.read_diagnostics(None))
        self.assertEqual(events, [])

    def test_rotation_creates_archive(self) -> None:
        diagnostics.log_diagnostic(self.state, "info", "first", {"detail": "x"})
        path = self.state / diagnostics.DIAGNOSTICS_FILE
        with unittest.mock.patch.object(diagnostics, "DIAGNOSTICS_MAX_BYTES", 10):
            diagnostics.log_diagnostic(self.state, "info", "second", {"detail": "y"})
        asyncio.run(diagnostics.read_diagnostics(self.state))
        self.assertTrue((self.state / "diagnostics.1.jsonl").exists())
        self.assertEqual(
            path.read_text(encoding="utf-8").strip().count("\n"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
