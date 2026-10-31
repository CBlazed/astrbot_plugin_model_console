"""console.backup 的单元测试：真实 SQLite 备份、manifest 校验、读取与保留策略。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from console import backup
from console.errors import ConsoleError
from console.engine import revision


def make_data_dir(root: Path, *, config: dict | None = None) -> Path:
    """构造一个最小的 AstrBot data 目录（cmd_config.json + config/ + data_v4.db）。"""
    data = root / "data"
    (data / "config").mkdir(parents=True, exist_ok=True)
    payload = config if config is not None else {"config_version": "1.0", "provider": []}
    (data / "cmd_config.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (data / "config" / "meme_manager_config.json").write_text(
        json.dumps({"semantic": {"vision_provider_id": "p1"}}), encoding="utf-8"
    )
    connection = sqlite3.connect(data / "data_v4.db")
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        connection.execute("INSERT INTO t (v) VALUES ('seed')")
        connection.commit()
    finally:
        connection.close()
    return data


class _BackupCase(unittest.IsolatedAsyncioTestCase):
    """带 data/state 临时目录的测试基类。"""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = make_data_dir(self.root)
        self.state = self.root / "state"
        self.state.mkdir()

    def backups_dir(self) -> Path:
        """备份根目录。"""
        return self.state / backup.BACKUPS_SUBDIR


class LimitsTest(unittest.TestCase):
    """保留上限的配置解析。"""

    def test_defaults(self) -> None:
        self.assertEqual(backup.backup_keep({}), 10)
        self.assertEqual(backup.backup_max_bytes({}), 2048 * 1024 * 1024)
        self.assertEqual(
            backup.backup_limits({}), {"keep": 10, "maxBytes": 2048 * 1024 * 1024}
        )

    def test_config_values(self) -> None:
        self.assertEqual(backup.backup_keep({"backup_keep": 3}), 3)
        self.assertEqual(backup.backup_max_bytes({"backup_max_mb": 10}), 10 * 1024 * 1024)

    def test_zero_means_unlimited(self) -> None:
        self.assertEqual(backup.backup_max_bytes({"backup_max_mb": 0}), 0)

    def test_invalid_falls_back(self) -> None:
        self.assertEqual(backup.backup_keep({"backup_keep": 0}), 10)
        self.assertEqual(backup.backup_keep({"backup_keep": "3"}), 10)
        self.assertEqual(backup.backup_keep({"backup_keep": True}), 10)
        self.assertEqual(backup.backup_max_bytes({"backup_max_mb": -5}), 2048 * 1024 * 1024)
        self.assertEqual(backup.backup_max_bytes({"backup_max_mb": "x"}), 2048 * 1024 * 1024)


class EnsureBackupTest(_BackupCase):
    """一次备份的产物与失败语义。"""

    async def test_creates_manifest_and_sqlite_copy(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")

        self.assertRegex(created["id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(created["sqlite"], "ok")
        self.assertEqual(created["files"], 2)
        directory = self.backups_dir() / created["id"]
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sqlite"], "ok")
        names = [item["name"] for item in manifest["files"]]
        self.assertEqual(names, ["cmd_config.json", "config/meme_manager_config.json"])
        for item in manifest["files"]:
            copied = directory / item["name"]
            self.assertTrue(copied.is_file())
            self.assertEqual(len(item["sha256"]), 64)
        # 备份内的 SQLite 数据库可用
        connection = sqlite3.connect(directory / "data_v4.db")
        try:
            row = connection.execute("SELECT v FROM t").fetchone()
        finally:
            connection.close()
        self.assertEqual(row[0], "seed")

    async def test_missing_sqlite_blocks_backup(self) -> None:
        (self.data / "data_v4.db").unlink()
        with self.assertRaises(ConsoleError) as caught:
            await backup.ensure_backup(self.data, self.state, "apply")
        self.assertEqual(caught.exception.code, "BACKUP_SQLITE_INVALID")
        # 没有 manifest.json 的目录不算可用备份
        self.assertEqual(await backup.list_backups(self.state), [])

    async def test_missing_cmd_config_blocks_backup(self) -> None:
        (self.data / "cmd_config.json").unlink()
        with self.assertRaises(ConsoleError) as caught:
            await backup.ensure_backup(self.data, self.state, "apply")
        self.assertEqual(caught.exception.code, "OPERATION_FAILED")

    async def test_list_and_stats(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        listed = await backup.list_backups(self.state)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], created["id"])
        self.assertEqual(listed[0]["files"], 2)
        self.assertEqual(listed[0]["sqlite"], "ok")
        self.assertGreater(listed[0]["bytes"], 0)
        stats = await backup.backup_stats(self.state)
        self.assertEqual(stats["count"], 1)
        self.assertEqual(len(stats["items"]), 1)

    async def test_list_empty_when_no_backups_dir(self) -> None:
        self.assertEqual(await backup.list_backups(self.state), [])
        stats = await backup.backup_stats(self.state)
        self.assertEqual(stats, {"count": 0, "bytes": 0, "items": []})


class ReadBackupResourceTest(_BackupCase):
    """从备份读回单个资源。"""

    async def test_reads_default_profile(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        stored = await backup.read_backup_resource(
            self.state, created["id"], {"kind": "profile", "id": "default"}
        )
        self.assertEqual(stored["file"], "cmd_config.json")
        self.assertEqual(stored["config"]["config_version"], "1.0")

    async def test_reads_plugin_config(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        stored = await backup.read_backup_resource(
            self.state, created["id"], {"kind": "plugin", "id": "meme_manager"}
        )
        self.assertEqual(stored["file"], "config/meme_manager_config.json")
        self.assertEqual(stored["config"]["semantic"]["vision_provider_id"], "p1")

    async def test_provider_lookup(self) -> None:
        data = make_data_dir(
            self.root,
            config={"provider": [{"id": "p1", "model": "m1"}, {"id": "p2", "model": "m2"}]},
        )
        created = await backup.ensure_backup(data, self.state, "apply")
        stored = await backup.read_backup_resource(
            self.state, created["id"], {"kind": "provider", "id": "p2"}
        )
        self.assertEqual(stored["config"], {"id": "p2", "model": "m2"})
        with self.assertRaises(ConsoleError) as caught:
            await backup.read_backup_resource(
                self.state, created["id"], {"kind": "provider", "id": "missing"}
            )
        self.assertEqual(caught.exception.code, "RESTORE_BACKUP_NOT_FOUND")

    async def test_invalid_backup_id(self) -> None:
        with self.assertRaises(ConsoleError) as caught:
            await backup.read_backup_resource(
                self.state, "../../etc", {"kind": "profile", "id": "default"}
            )
        self.assertEqual(caught.exception.code, "RESTORE_BACKUP_NOT_FOUND")

    async def test_traversal_resource_rejected(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        with self.assertRaises(ConsoleError) as caught:
            await backup.read_backup_resource(
                self.state, created["id"], {"kind": "plugin", "id": "../evil"}
            )
        self.assertEqual(caught.exception.code, "RESTORE_BACKUP_INVALID")


class PruneTest(_BackupCase):
    """保留策略：按份数清理，保护最新一份。"""

    async def test_prune_keeps_newest(self) -> None:
        first = await backup.ensure_backup(self.data, self.state, "apply")
        await asyncio.sleep(0.02)
        second = await backup.ensure_backup(self.data, self.state, "apply")
        result = await backup.prune_backups(self.state, 1, 0)
        self.assertEqual(len(result["removed"]), 1)
        self.assertTrue(result["removed"][0].endswith(first["id"]))
        remaining = await backup.list_backups(self.state)
        self.assertEqual([item["id"] for item in remaining], [second["id"]])

    async def test_protect_wins_even_if_older(self) -> None:
        first = await backup.ensure_backup(self.data, self.state, "apply")
        await asyncio.sleep(0.02)
        second = await backup.ensure_backup(self.data, self.state, "apply")
        protected = str(self.backups_dir() / first["id"])
        result = await backup.prune_backups(self.state, 1, 0, protect=protected)
        kept_ids = {Path(path).name for path in result["kept"]}
        self.assertIn(first["id"], kept_ids)
        self.assertNotIn(second["id"], kept_ids)

    async def test_invalid_keep_falls_back(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        result = await backup.prune_backups(self.state, "bad", 0)
        self.assertEqual(len(result["removed"]), 0)
        self.assertTrue(Path(result["kept"][0]).name == created["id"])


class IntegrityTest(_BackupCase):
    """清单与内容一致性。"""

    async def test_manifest_sha_matches_file(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        directory = self.backups_dir() / created["id"]
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        import hashlib

        for item in manifest["files"]:
            digest = hashlib.sha256((directory / item["name"]).read_bytes()).hexdigest()
            self.assertEqual(digest, item["sha256"])

    async def test_revision_of_backup_matches_original(self) -> None:
        created = await backup.ensure_backup(self.data, self.state, "apply")
        stored = await backup.read_backup_resource(
            self.state, created["id"], {"kind": "profile", "id": "default"}
        )
        original = json.loads((self.data / "cmd_config.json").read_text(encoding="utf-8"))
        self.assertEqual(revision(stored["config"]), revision(original))


if __name__ == "__main__":
    unittest.main()
