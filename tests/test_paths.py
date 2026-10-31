"""console.paths 的单元测试：状态目录解析、可写探针、data 目录发现与旧状态导入。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console import paths
from console.errors import ConsoleError

_ENV_KEYS = (
    "MODEL_CONSOLE_STATE",
    "ASTRBOT_DATA",
    "LOCALAPPDATA",
    "XDG_STATE_HOME",
    "USERPROFILE",
    "HOME",
)


class _EnvIsolation(unittest.TestCase):
    """把相关环境变量清空，避免宿主机环境污染断言。"""

    def setUp(self) -> None:
        filtered = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS}
        self._env = patch.dict(os.environ, filtered, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class ResolveStateDirTest(_EnvIsolation):
    """resolve_state_dir 的优先级。"""

    def test_env_wins(self) -> None:
        os.environ["MODEL_CONSOLE_STATE"] = str(self.root / "from-env")
        resolved = paths.resolve_state_dir(
            {"state_dir": str(self.root / "from-config")},
            default=self.root / "plugin" / "state",
        )
        self.assertEqual(resolved, (self.root / "from-env").resolve())

    def test_config_overrides_default(self) -> None:
        resolved = paths.resolve_state_dir(
            {"state_dir": str(self.root / "from-config")},
            default=self.root / "plugin" / "state",
        )
        self.assertEqual(resolved, (self.root / "from-config").resolve())

    def test_relative_config_resolves_against_plugin_data(self) -> None:
        plugin_data = self.root / "plugin_data" / "astrbot_plugin_model_console"
        resolved = paths.resolve_state_dir(
            {"state_dir": "custom-state"},
            default=plugin_data / "state",
        )
        self.assertEqual(resolved, (plugin_data / "custom-state").resolve())

    def test_default_used_when_nothing_set(self) -> None:
        default = self.root / "plugin" / "state"
        self.assertEqual(paths.resolve_state_dir({}, default=default), default.resolve())

    def test_blank_config_falls_through(self) -> None:
        default = self.root / "plugin" / "state"
        self.assertEqual(
            paths.resolve_state_dir({"state_dir": "   "}, default=default),
            default.resolve(),
        )


class EnsureWritableTest(_EnvIsolation):
    """ensure_writable_state_dir 的通过/拒绝路径。"""

    def test_creates_and_probes(self) -> None:
        target = self.root / "deep" / "state"
        paths.ensure_writable_state_dir(target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.glob(".write-probe-*")), [])

    def test_rejects_unwritable_path(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ConsoleError) as caught:
            paths.ensure_writable_state_dir(blocker / "state")
        self.assertEqual(caught.exception.code, "STATE_DIR_NOT_WRITABLE")
        self.assertIn("failures", caught.exception.details)


class DiscoverDataTest(_EnvIsolation):
    """discover_astrbot_data 的发现规则。"""

    def _make_data_dir(self, name: str = "data") -> Path:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cmd_config.json").write_text("{}", encoding="utf-8")
        return directory

    def test_env_override(self) -> None:
        data = self._make_data_dir()
        os.environ["ASTRBOT_DATA"] = str(data)
        self.assertEqual(paths.discover_astrbot_data({}), data.resolve())

    def test_config_override(self) -> None:
        data = self._make_data_dir()
        self.assertEqual(
            paths.discover_astrbot_data({"astrbot_data": str(data)}),
            data.resolve(),
        )

    def test_missing_cmd_config_rejected(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ConsoleError) as caught:
            paths.discover_astrbot_data({"astrbot_data": str(empty)})
        self.assertEqual(caught.exception.code, "ASTRBOT_DATA_REQUIRED")

    def test_no_candidates_raises(self) -> None:
        os.environ["USERPROFILE"] = str(self.root / "home")
        os.environ["HOME"] = str(self.root / "home")
        with self.assertRaises(ConsoleError) as caught:
            paths.discover_astrbot_data({})
        self.assertEqual(caught.exception.code, "ASTRBOT_DATA_REQUIRED")
        self.assertEqual(caught.exception.details.get("candidates"), 0)

    def test_single_launcher_candidate_discovered(self) -> None:
        home = self.root / "home"
        candidate = home / ".astrbot_launcher" / "instances" / "abc" / "core" / "data"
        candidate.mkdir(parents=True)
        (candidate / "cmd_config.json").write_text("{}", encoding="utf-8")
        os.environ["USERPROFILE"] = str(home)
        os.environ["HOME"] = str(home)
        self.assertEqual(paths.discover_astrbot_data({}), candidate.resolve())


class LegacyStateTest(_EnvIsolation):
    """legacy_state_dir / import_legacy_state 的语义。"""

    def test_legacy_absent(self) -> None:
        os.environ["LOCALAPPDATA"] = str(self.root / "local")
        self.assertIsNone(paths.legacy_state_dir())

    def test_legacy_present(self) -> None:
        local = self.root / "local"
        legacy = local / "AstrBotModelConsole" / "state"
        legacy.mkdir(parents=True)
        os.environ["LOCALAPPDATA"] = str(local)
        self.assertEqual(paths.legacy_state_dir(), legacy)


class LegacyImportRecordTest(_EnvIsolation, unittest.IsolatedAsyncioTestCase):
    """导入记录的原子读写与「旧目录是否存在且非空」。"""

    async def test_record_roundtrip(self) -> None:
        record = {
            "at": "2026-01-01T00:00:00.000Z",
            "source": str(self.root / "legacy"),
            "copied": 3,
            "skipped": 1,
            "files": 4,
            "status": "imported",
            "imported": True,
        }
        await paths.write_legacy_import_record(self.root / "state", record)
        self.assertEqual(
            await paths.read_legacy_import_record(self.root / "state"),
            record,
        )
        self.assertEqual(
            list((self.root / "state").glob(".legacy-import.json.*.tmp")), []
        )

    async def test_missing_or_corrupt_record(self) -> None:
        self.assertIsNone(await paths.read_legacy_import_record(self.root / "state"))
        state = self.root / "state"
        state.mkdir()
        (state / paths.LEGACY_IMPORT_RECORD).write_text("{not json", encoding="utf-8")
        self.assertIsNone(await paths.read_legacy_import_record(state))
        (state / paths.LEGACY_IMPORT_RECORD).write_text("[]", encoding="utf-8")
        self.assertIsNone(await paths.read_legacy_import_record(state))

    async def test_legacy_state_present(self) -> None:
        legacy = self.root / "legacy"
        legacy.mkdir()
        self.assertFalse(await paths.legacy_state_present(legacy))
        (legacy / "devices.json").write_text("{}", encoding="utf-8")
        self.assertTrue(await paths.legacy_state_present(legacy))
        self.assertFalse(await paths.legacy_state_present(self.root / "missing"))
        os.environ["LOCALAPPDATA"] = str(self.root / "local")
        self.assertFalse(await paths.legacy_state_present(None))


class ImportLegacyStateTest(_EnvIsolation, unittest.IsolatedAsyncioTestCase):
    """import_legacy_state 复制语义：不覆盖、不删源。"""

    async def test_copy_without_overwrite(self) -> None:
        legacy = self.root / "legacy"
        (legacy / "backups" / "one").mkdir(parents=True)
        (legacy / "backups" / "one" / "manifest.json").write_text(
            json.dumps({"files": []}), encoding="utf-8"
        )
        (legacy / "devices.json").write_text('{"devices":[]}', encoding="utf-8")
        target = self.root / "target"
        target.mkdir()
        (target / "devices.json").write_text('{"devices":[{"id":"local"}]}', encoding="utf-8")

        result = await paths.import_legacy_state(legacy, target)

        self.assertTrue(result["imported"])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue((target / "backups" / "one" / "manifest.json").is_file())
        self.assertEqual(
            (target / "devices.json").read_text(encoding="utf-8"),
            '{"devices":[{"id":"local"}]}',
        )
        self.assertTrue((legacy / "devices.json").is_file())

    async def test_missing_source_is_noop(self) -> None:
        result = await paths.import_legacy_state(self.root / "nope", self.root / "target")
        self.assertFalse(result["imported"])
        self.assertEqual(result["files"], 0)


if __name__ == "__main__":
    unittest.main()
