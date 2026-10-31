"""``console.devices`` 单元测试：校验规则、去重、凭据隔离、DPAPI 降级与迁移。

全部使用临时目录与注入的假 DPAPI runner，不读写真实状态目录、不发网络请求。
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from console import devices
from console.errors import ConsoleError
from console.secure_store import SECURE_STORE_UNAVAILABLE

AGENT_TOKEN = "a" * 64


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


async def fake_runner(operation: str, payload: str) -> str:
    """可逆的 ``FAKE:`` 前缀 runner。"""
    raw = base64.b64decode(payload)
    if operation == "protect":
        return base64.b64encode(b"FAKE:" + raw).decode("ascii")
    if operation == "unprotect":
        if not raw.startswith(b"FAKE:"):
            raise ValueError("FAKE_BAD_BLOB")
        return base64.b64encode(raw[5:]).decode("ascii")
    raise ValueError("FAKE_UNKNOWN_OPERATION")


async def failing_runner(_operation: str, _payload: str) -> str:
    """模拟 DPAPI 不可用。"""
    raise RuntimeError("dpapi unavailable")


class DeviceTestCase(unittest.IsolatedAsyncioTestCase):
    """公共临时目录与文件读取辅助。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="model-console-devices-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_json(self, name: str) -> dict:
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def read_text(self, name: str) -> str:
        return (self.dir / name).read_text(encoding="utf-8")

    def assert_code(self, code: str, callback, *args, **kwargs) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            callback(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)


class ValidateDeviceTests(DeviceTestCase):
    """``validate_device`` 的规则与规范化。"""

    def test_rejects_bad_names_urls_protocols_kinds_and_ids(self) -> None:
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {"name": "", "baseUrl": "http://h:1"})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {"name": "x", "baseUrl": "not-a-url"})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {"name": "x", "baseUrl": "ftp://h:1"})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {"name": "x", "baseUrl": "http://user:pass@h:1"})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {"name": "x", "baseUrl": "http://h:1", "kind": "ftp"})
        self.assert_code("DEVICE_EXISTS", devices.validate_device,
                         {"name": "x", "baseUrl": "http://h:1", "id": "a"},
                         existing_ids=["a"])

    def test_normalizes_name_url_and_defaults(self) -> None:
        device = devices.validate_device(
            {"name": "  书房  ", "baseUrl": "http://192.168.1.20:6185/", "readOnly": True})
        self.assertEqual(device["name"], "书房")
        self.assertEqual(device["kind"], "http")
        self.assertEqual(device["baseUrl"], "http://192.168.1.20:6185")
        self.assertTrue(device["readOnly"])
        self.assertEqual(device["auth"], {"type": "api-key"})
        self.assertTrue(device["id"])
        self.assertTrue(device["createdAt"].endswith("Z"))

    def test_readonly_defaults_true_and_false_is_kept(self) -> None:
        default = devices.validate_device({"name": "x", "baseUrl": "http://h:1"})
        self.assertTrue(default["readOnly"])
        writable = devices.validate_device(
            {"name": "x", "baseUrl": "http://h:2", "readOnly": False})
        self.assertFalse(writable["readOnly"])

    def test_ssh_devices_require_host_user_and_keep_optional_fields(self) -> None:
        base = {"name": "x", "kind": "ssh", "baseUrl": "http://127.0.0.1:1"}
        self.assert_code("DEVICE_INVALID", devices.validate_device, base)
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {**base, "ssh": {"host": "", "user": "u"}})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {**base, "ssh": {"host": "h"}})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {**base, "ssh": {"host": "h", "user": "u", "port": 0}})
        self.assert_code("DEVICE_INVALID", devices.validate_device,
                         {**base, "ssh": {"host": "h", "user": "u", "port": "22"}})
        device = devices.validate_device({
            "name": "云主机", "kind": "ssh", "baseUrl": "http://127.0.0.1:6185",
            "ssh": {"host": "1.2.3.4", "port": 2222, "user": "Administrator",
                    "keyPath": "C:\\keys\\id_ed25519", "dataPath": "C:\\AstrBot\\data"},
        })
        self.assertEqual(device["ssh"], {
            "host": "1.2.3.4", "user": "Administrator", "port": 2222,
            "keyPath": "C:\\keys\\id_ed25519", "dataPath": "C:\\AstrBot\\data",
        })
        self.assertEqual(device["auth"], {"type": "api-key"})

    def test_agent_devices_never_persist_pairing_code(self) -> None:
        device = devices.validate_device({
            "name": "Agent", "kind": "agent", "baseUrl": "http://127.0.0.1:9000",
            "pairingCode": "12345678",
        })
        self.assertEqual(device["kind"], "agent")
        self.assertEqual(device["auth"], {"type": "agent-token"})
        self.assertNotIn("pairingCode", device)
        self.assertNotIn("12345678", json.dumps(device))

    def test_duplicates_compare_kind_and_normalized_base_url(self) -> None:
        existing = [{"kind": "http", "baseUrl": "http://h:1/"}]
        self.assert_code("DEVICE_EXISTS", devices.validate_device,
                         {"name": "dup", "baseUrl": "http://h:1"},
                         existing_devices=existing)
        self.assert_code("DEVICE_EXISTS", devices.validate_device,
                         {"name": "dup", "baseUrl": "HTTP://H:1"},
                         existing_devices=existing)
        devices.validate_device({"name": "other-kind", "kind": "agent",
                                 "baseUrl": "http://h:1"}, existing_devices=existing)
        devices.validate_device({"name": "other-url", "baseUrl": "http://h:2"},
                                existing_devices=existing)


class RegistryTests(DeviceTestCase):
    """注册表 + 凭据存储的端到端行为。"""

    async def test_add_device_stores_secret_outside_devices_json(self) -> None:
        device = await devices.add_device(
            self.dir, {"name": "书房主机", "baseUrl": "http://192.168.1.20:6185"},
            "abk_secret_value", runner=fake_runner)
        self.assertTrue(device["hasSecret"])
        self.assertNotIn("abk_secret_value", self.read_text("devices.json"))
        secrets = self.read_json("secrets.json")
        self.assertEqual(secrets["version"], devices.SECRETS_VERSION)
        self.assertRegex(secrets["secrets"][device["id"]], r"^dpapi:")
        self.assertNotIn("abk_secret_value", json.dumps(secrets))
        self.assertEqual(
            await devices.get_secret(self.dir, device["id"], runner=fake_runner),
            "abk_secret_value")
        listed = await devices.list_devices(self.dir)
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["hasSecret"])
        self.assertNotIn("abk_secret_value", json.dumps(listed))
        with self.assertRaises(ConsoleError) as ctx:
            await devices.add_device(self.dir,
                                     {"name": "second", "baseUrl": "http://h:2"}, "",
                                     runner=fake_runner)
        self.assertEqual(ctx.exception.code, "DEVICE_SECRET_REQUIRED")
        self.assertIn("OpenAPI Key", ctx.exception.message)

    async def test_ssh_and_agent_records_are_sanitized_on_disk(self) -> None:
        ssh = await devices.add_device(
            self.dir,
            {"name": "云主机", "kind": "ssh", "baseUrl": "http://127.0.0.1:1",
             "ssh": {"host": "10.0.0.2", "user": "root", "port": 22}},
            "abk_ssh_key", runner=fake_runner)
        self.assertEqual(ssh["ssh"], {"host": "10.0.0.2", "user": "root", "port": 22})
        agent = await devices.add_device(
            self.dir,
            {"name": "Agent", "kind": "agent", "baseUrl": "http://127.0.0.1:9000",
             "pairingCode": "87654321"},
            AGENT_TOKEN, runner=fake_runner)
        self.assertEqual(
            await devices.get_secret(self.dir, agent["id"], runner=fake_runner),
            AGENT_TOKEN)
        secrets_text = json.dumps(self.read_json("secrets.json"))
        devices_text = self.read_text("devices.json")
        for leaked in ("abk_ssh_key", AGENT_TOKEN, "87654321"):
            self.assertNotIn(leaked, secrets_text)
            self.assertNotIn(leaked, devices_text)
        listed = await devices.list_devices(self.dir)
        self.assertNotIn(AGENT_TOKEN, json.dumps(listed))
        self.assertNotIn("87654321", json.dumps(listed))
        with self.assertRaises(ConsoleError) as ctx:
            await devices.add_device(
                self.dir, {"name": "Agent2", "kind": "agent",
                           "baseUrl": "http://127.0.0.1:9001"}, "",
                runner=fake_runner)
        self.assertIn("token", ctx.exception.message)

    async def test_update_keeps_old_secret_unless_new_key_given(self) -> None:
        device = await devices.add_device(
            self.dir, {"name": "a", "baseUrl": "http://h:1"}, "abk_one",
            runner=fake_runner)
        await devices.update_device(self.dir, device["id"], {"name": "b"},
                                    runner=fake_runner)
        self.assertEqual(await devices.get_secret(self.dir, device["id"],
                                                  runner=fake_runner), "abk_one")
        updated = await devices.update_device(self.dir, device["id"],
                                             {"apiKey": "abk_two"}, runner=fake_runner)
        self.assertEqual(updated["name"], "b")
        self.assertEqual(await devices.get_secret(self.dir, device["id"],
                                                  runner=fake_runner), "abk_two")
        self.assertNotIn("abk_two", json.dumps(self.read_json("secrets.json")))

    async def test_remove_device_clears_registry_and_secret(self) -> None:
        device = await devices.add_device(
            self.dir, {"name": "a", "baseUrl": "http://h:1"}, "abk_one",
            runner=fake_runner)
        await devices.remove_device(self.dir, device["id"])
        self.assertEqual((await devices.load_registry(self.dir))["devices"], [])
        self.assertIsNone(await devices.get_secret(self.dir, device["id"],
                                                   runner=fake_runner))
        with self.assertRaises(ConsoleError) as ctx:
            await devices.remove_device(self.dir, device["id"])
        self.assertEqual(ctx.exception.code, "DEVICE_NOT_FOUND")
        self.assertIn("设备不存在", ctx.exception.message)

    async def test_duplicate_add_and_unknown_update_errors(self) -> None:
        await devices.add_device(self.dir, {"name": "a", "baseUrl": "http://h:1"},
                                 "abk", runner=fake_runner)
        with self.assertRaises(ConsoleError) as ctx:
            await devices.add_device(
                self.dir, {"name": "b", "baseUrl": "http://h:1/"}, "abk2",
                runner=fake_runner)
        self.assertEqual(ctx.exception.code, "DEVICE_EXISTS")
        with self.assertRaises(ConsoleError) as ctx:
            await devices.update_device(self.dir, "missing", {"name": "x"},
                                        runner=fake_runner)
        self.assertEqual(ctx.exception.code, "DEVICE_NOT_FOUND")

    async def test_legacy_plaintext_is_readable_and_migrates_to_dpapi(self) -> None:
        (self.dir / "secrets.json").write_text(json.dumps(
            {"version": 1, "secrets": {"old": "abk_legacy", "other": "plain-value"}}),
            encoding="utf-8")
        self.assertEqual(await devices.get_secret(self.dir, "old", runner=fake_runner),
                         "abk_legacy")
        report = await devices.migrate_secrets(self.dir, runner=fake_runner)
        self.assertEqual(sorted(report["migrated"]), ["old", "other"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["plaintextRemaining"], 0)
        stored = self.read_json("secrets.json")
        self.assertEqual(stored["version"], devices.SECRETS_VERSION)
        self.assertRegex(stored["secrets"]["old"], r"^dpapi:")
        self.assertRegex(stored["secrets"]["other"], r"^dpapi:")
        self.assertNotIn("abk_legacy", json.dumps(stored))
        self.assertNotIn("plain-value", json.dumps(stored))

    async def test_migration_failure_keeps_plaintext_per_device(self) -> None:
        (self.dir / "secrets.json").write_text(json.dumps(
            {"version": 1, "secrets": {"keep": "abk_keep", "convert": "abk_convert"}}),
            encoding="utf-8")

        async def selective(operation: str, payload: str) -> str:
            if operation == "protect" and base64.b64decode(payload) == b"abk_keep":
                raise RuntimeError("dpapi failed")
            return await fake_runner(operation, payload)

        report = await devices.migrate_secrets(self.dir, runner=selective)
        self.assertEqual(report["migrated"], ["convert"])
        self.assertEqual(report["failed"],
                         [{"id": "keep", "code": SECURE_STORE_UNAVAILABLE}])
        self.assertEqual(report["plaintextRemaining"], 1)
        stored = self.read_json("secrets.json")
        self.assertEqual(stored["secrets"]["keep"], "abk_keep")
        self.assertRegex(stored["secrets"]["convert"], r"^dpapi:")

    async def test_dpapi_unavailable_degrades_to_plaintext_with_diagnostic(self) -> None:
        with mock.patch.object(devices, "_record_fallback") as record:
            device = await devices.add_device(
                self.dir, {"name": "降级主机", "baseUrl": "http://h:9"},
                "abk_fallback", runner=failing_runner)
            record.assert_called_once_with(self.dir)
        self.assertTrue(device["hasSecret"])
        self.assertEqual(await devices.get_secret(self.dir, device["id"]),
                         "abk_fallback")
        stored = self.read_json("secrets.json")
        self.assertEqual(stored["version"], devices.SECRETS_VERSION)
        self.assertEqual(stored["secrets"][device["id"]], "abk_fallback")
        self.assertTrue((await devices.list_devices(self.dir))[0]["hasSecret"])
        report = await devices.set_secret(self.dir, "extra", "plain_value",
                                          runner=failing_runner)
        self.assertEqual(report, {"protected": False, "code": SECURE_STORE_UNAVAILABLE})
        self.assertEqual(await devices.set_secret(self.dir, "extra2", "stored_value",
                                                  runner=fake_runner),
                         {"protected": True})

    async def test_migrating_missing_or_dpapi_only_file_is_noop(self) -> None:
        self.assertEqual(await devices.migrate_secrets(self.dir, runner=fake_runner),
                         {"migrated": [], "failed": [], "plaintextRemaining": 0})
        (self.dir / "secrets.json").write_text(
            json.dumps({"version": 2, "secrets": {"done": "dpapi:AAAA"}}),
            encoding="utf-8")
        self.assertEqual(await devices.migrate_secrets(self.dir, runner=fake_runner),
                         {"migrated": [], "failed": [], "plaintextRemaining": 0})

    async def test_corrupt_ciphertext_fails_closed_and_legacy_still_reads(self) -> None:
        (self.dir / "secrets.json").write_text(json.dumps({
            "version": 2,
            "secrets": {"bad": "dpapi:AAAA", "worse": "dpapi:%%%",
                        "legacy": "plain_but_ok"},
        }), encoding="utf-8")
        from console.secure_store import SecureStoreUnavailable

        with self.assertRaises(SecureStoreUnavailable):
            await devices.get_secret(self.dir, "bad", runner=failing_runner)
        with self.assertRaises(SecureStoreUnavailable):
            await devices.get_secret(self.dir, "worse", runner=fake_runner)
        self.assertEqual(await devices.get_secret(self.dir, "legacy", runner=fake_runner),
                         "plain_but_ok")
        report = await devices.migrate_secrets(self.dir, runner=fake_runner)
        self.assertIn("legacy", report["migrated"])

    async def test_load_registry_filters_malformed_records(self) -> None:
        (self.dir / "devices.json").write_text(json.dumps({
            "version": 1,
            "devices": [{"id": "ok", "name": "a", "kind": "http",
                         "baseUrl": "http://h:1"},
                        {"name": "no-id"}, "junk"],
        }), encoding="utf-8")
        registry = await devices.load_registry(self.dir)
        self.assertEqual([item["id"] for item in registry["devices"]], ["ok"])
        empty = await devices.load_registry(self.dir / "missing")
        self.assertEqual(empty, {"version": 1, "devices": []})


class PublicDeviceTests(DeviceTestCase):
    """``public_device`` 白名单：凭据与本地路径绝不外泄。"""

    def test_whitelist_hides_secrets_and_local_paths(self) -> None:
        device = {
            "id": "d1", "name": "云主机", "kind": "ssh",
            "baseUrl": "http://127.0.0.1:6185", "readOnly": False,
            "auth": {"type": "api-key"}, "hasSecret": True,
            "createdAt": "2026-10-29T10:00:00.000Z",
            "ssh": {"host": "10.0.0.2", "user": "root", "port": 2222,
                    "keyPath": "C:\\keys\\id_ed25519",
                    "dataPath": "C:\\AstrBot\\data"},
        }
        view = devices.public_device(device)
        self.assertEqual(view, {
            "id": "d1", "name": "云主机", "kind": "ssh", "readOnly": False,
            "hasSecret": True, "baseUrl": "http://127.0.0.1:6185",
            "ssh": {"host": "10.0.0.2", "user": "root", "port": 2222},
            "createdAt": "2026-10-29T10:00:00.000Z",
        })
        serialized = json.dumps(view)
        self.assertNotIn("keyPath", serialized)
        self.assertNotIn("dataPath", serialized)
        self.assertNotIn("auth", serialized)

    def test_defaults_for_minimal_record(self) -> None:
        view = devices.public_device({"id": "x", "name": "n"})
        self.assertEqual(view["kind"], "http")
        self.assertTrue(view["readOnly"])
        self.assertFalse(view["hasSecret"])
        self.assertNotIn("baseUrl", view)


if __name__ == "__main__":
    unittest.main()
