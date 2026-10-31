"""console.app 的单元测试：端点编排、参数校验、只读门禁与响应白名单。

用假 Gateway / 假 Engine + 临时目录，不触碰真实 AstrBot。
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from console import app as app_module
from console import backup as backup_module
from console import diagnostics
from console import paths
from console.app import (
    ConsoleApp,
    backup_id_of,
    normalize_stats,
    parse_resource,
    public_backup,
    public_changes,
    public_health,
    public_restore_plan,
    safe_apply_result,
)
from console.errors import ConsoleError

_ENV_KEYS = (
    "MODEL_CONSOLE_STATE",
    "ASTRBOT_DATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOME",
    "XDG_STATE_HOME",
    "DASHBOARD_PORT",
    "ASTRBOT_DASHBOARD_PORT",
)

BACKUP_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_UUID = "22222222-2222-2222-2222-222222222222"


class FakeGateway:
    """内存版网关（App 只需要 read / efforts / close）。"""

    instances: list["FakeGateway"] = []

    def __init__(self, config, data_dir=None, **kwargs: object) -> None:
        self.config = config
        self.data_dir = data_dir
        self.read_only = False
        self.efforts_value: list | None = ["low", "high"]
        self.profiles: dict[str, dict] = {}
        self.closed = False
        FakeGateway.instances.append(self)

    def set_read_only(self, value: bool) -> None:
        self.read_only = bool(value)

    async def read(self, resource: dict) -> dict:
        key = resource.get("id")
        if key not in self.profiles:
            raise ConsoleError("ASTRBOT_HTTP_404")
        return copy.deepcopy(self.profiles[key])

    async def efforts(self, provider_id: str) -> list | None:
        return copy.deepcopy(self.efforts_value)

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    """记录调用并返回可控结果的引擎替身。"""

    instance: "FakeEngine | None" = None

    def __init__(self, gateway, state_dir, data_dir, limits, read_only) -> None:
        self.gateway = gateway
        self.state_dir = state_dir
        self.data_dir = data_dir
        self.limits = limits
        self.read_only = read_only
        self.calls: list[tuple] = []
        self.snapshot_payload: dict = {
            "readAt": "2026-01-01T00:00:00.000Z",
            "writeEnabled": not read_only,
            "configVersion": None,
            "coverage": "coverage",
            "routing": {"total": 0, "platformLevel": 0, "granular": 0},
            "plugins": None,
            "features": {},
            "models": [],
            "resources": [],
        }
        self.backups: list[dict] = []
        self.stats: dict = {"count": 0, "bytes": 0, "items": []}
        self.backup_diff_result: dict = {
            "backupId": BACKUP_UUID,
            "resource": {"kind": "profile", "id": "default"},
            "changes": [
                {
                    "path": "provider_settings.web_search",
                    "before": "false",
                    "value": "true",
                    "capability": "setting",
                    "secret": True,
                }
            ],
            "currentRevision": "current-rev",
        }
        self.plan_restore_result: dict = {
            "id": "33333333-3333-3333-3333-333333333333",
            "resource": {"kind": "profile", "id": "default"},
            "revision": "rev",
            "changes": [
                {"path": "provider_settings.web_search", "before": "false", "value": "true"}
            ],
            "expiresAt": "2026-01-01T00:10:00.000Z",
            "impact": "this profile",
            "warning": "warning",
            "mode": "restore",
            "backupId": BACKUP_UUID,
        }
        self.apply_result: dict = {
            "status": "saved-and-readback-verified",
            "id": "plan-1",
            "backupId": f"/some/dir/{BACKUP_UUID}",
            "generationVerified": False,
            "hotLoadVerified": False,
        }
        self.plan_result: dict = {
            "id": "44444444-4444-4444-4444-444444444444",
            "resource": {"kind": "profile", "id": "default"},
            "revision": "rev",
            "changes": [],
            "expiresAt": "2026-01-01T00:10:00.000Z",
            "impact": "this profile",
            "warning": "warning",
        }
        FakeEngine.instance = self

    async def snapshot(self) -> dict:
        self.calls.append(("snapshot",))
        return copy.deepcopy(self.snapshot_payload)

    async def list_backups(self) -> list[dict]:
        self.calls.append(("list_backups",))
        return copy.deepcopy(self.backups)

    async def backup_stats(self) -> dict:
        self.calls.append(("backup_stats",))
        return copy.deepcopy(self.stats)

    async def backup_diff(self, backup_id: str, resource: dict) -> dict:
        self.calls.append(("backup_diff", backup_id, resource))
        result = copy.deepcopy(self.backup_diff_result)
        result["backupId"] = backup_id
        result["resource"] = resource
        return result

    async def plan_restore(
        self, backup_id: str, resource: dict, expected_revision: str | None = None
    ) -> dict:
        self.calls.append(("plan_restore", backup_id, resource, expected_revision))
        return copy.deepcopy(self.plan_restore_result)

    async def plan(
        self,
        resource: dict,
        changes: list,
        restore: bool = False,
        expected_revision: str | None = None,
    ) -> dict:
        self.calls.append(("plan", resource, changes, restore, expected_revision))
        return copy.deepcopy(self.plan_result)

    async def apply(self, plan_id: str) -> dict:
        self.calls.append(("apply", plan_id))
        return copy.deepcopy(self.apply_result)


class _AppCase(unittest.IsolatedAsyncioTestCase):
    """带临时目录的 App 测试基类。"""

    async def asyncSetUp(self) -> None:
        filtered = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS}
        self._env = patch.dict(os.environ, filtered, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        FakeGateway.instances = []
        FakeEngine.instance = None

    def make_data_dir(self, config: dict | None = None) -> Path:
        """构造最小 AstrBot data 目录。"""
        data = self.root / "data"
        (data / "config").mkdir(parents=True, exist_ok=True)
        (data / "cmd_config.json").write_text(
            json.dumps(config or {"dashboard": {"jwt_secret": "s", "username": "astrbot"}}),
            encoding="utf-8",
        )
        connection = sqlite3.connect(data / "data_v4.db")
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        return data

    async def make_app(self, config: dict | None = None) -> ConsoleApp:
        """创建并初始化 ConsoleApp（Gateway / Engine 均替换为替身）。"""
        data = self.make_data_dir()
        payload = {
            "astrbot_data": str(data),
            "state_dir": str(self.root / "state"),
            "edit_mode": True,
        }
        payload.update(config or {})
        app = ConsoleApp(payload, self.root / "plugin_data")
        with patch.object(app_module, "Gateway", FakeGateway), patch.object(
            app_module, "Engine", FakeEngine
        ):
            await app.initialize()
        return app

    def assert_code(self, code: str, coroutine_factory) -> None:
        """断言协程抛出指定错误码。"""
        with self.assertRaises(ConsoleError) as caught:
            self._run(coroutine_factory())
        self.assertEqual(caught.exception.code, code)

    def _run(self, coroutine):
        """同步执行协程（供 assert_code 使用）。"""
        import asyncio

        return asyncio.get_event_loop().run_until_complete(coroutine)


class HealthStateTest(_AppCase):
    """health / state 的运行态字段。"""

    async def test_health(self) -> None:
        app = await self.make_app()
        health = await app.health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["version"], "v0.1.0")
        self.assertEqual(health["dataPath"], str(self.root / "data"))
        self.assertEqual(health["stateDir"], str(self.root / "state"))
        self.assertFalse(health["readOnly"])
        self.assertIsNotNone(health["startedAt"])

    async def test_state(self) -> None:
        app = await self.make_app()
        state = await app.state()
        self.assertEqual(state["stateDir"], str(self.root / "state"))
        self.assertTrue(state["writable"])
        self.assertTrue(state["editMode"])
        self.assertFalse(state["legacyImported"])
        self.assertEqual(state["astrbotData"], str(self.root / "data"))

    async def test_read_only_from_config(self) -> None:
        app = await self.make_app({"edit_mode": False})
        health = await app.health()
        state = await app.state()
        self.assertTrue(health["readOnly"])
        self.assertFalse(state["editMode"])
        self.assertTrue(app.gateway.read_only)

    async def test_initialize_failure_keeps_app_alive(self) -> None:
        app = ConsoleApp(
            {"astrbot_data": str(self.root / "missing")},
            self.root / "plugin_data",
        )
        os.environ["USERPROFILE"] = str(self.root / "home")
        os.environ["HOME"] = str(self.root / "home")
        await app.initialize()
        health = await app.health()
        self.assertIsNone(health["dataPath"])
        with self.assertRaises(ConsoleError) as caught:
            await app.snapshot(None)
        self.assertEqual(caught.exception.code, "ASTRBOT_DATA_REQUIRED")

    async def test_close_releases_gateway(self) -> None:
        app = await self.make_app()
        gateway = app.gateway
        await app.close()
        self.assertTrue(gateway.closed)
        self.assertIsNone(app.gateway)


class ReadOnlyGateTest(_AppCase):
    """只读模式下写操作统一 READ_ONLY_MODE。"""

    async def test_write_endpoints_blocked(self) -> None:
        app = await self.make_app({"edit_mode": False})
        for factory in (
            lambda: app.plans({}),
            lambda: app.apply({"id": "x"}),
            lambda: app.restore_plan({}),
            lambda: app.add_device({}),
            lambda: app.migrate_state({}),
        ):
            with self.assertRaises(ConsoleError) as caught:
                await factory()
            self.assertEqual(caught.exception.code, "READ_ONLY_MODE")

    async def test_read_endpoints_allowed(self) -> None:
        app = await self.make_app({"edit_mode": False})
        self.assertEqual((await app.health())["status"], "ok")
        self.assertIn("devices", await app.devices())


class SnapshotTest(_AppCase):
    """GET /snapshot 的本地/远程分支。"""

    async def test_local_snapshot_adds_device(self) -> None:
        app = await self.make_app()
        snapshot = await app.snapshot(None)
        self.assertEqual(snapshot["device"], {"id": "local", "name": "本机", "kind": "local"})
        self.assertEqual(snapshot["readAt"], "2026-01-01T00:00:00.000Z")
        self.assertEqual((await app.snapshot("local"))["device"]["id"], "local")

    async def test_unknown_device(self) -> None:
        app = await self.make_app()
        with self.assertRaises(ConsoleError) as caught:
            await app.snapshot("ghost")
        self.assertEqual(caught.exception.code, "DEVICE_NOT_FOUND")


class CapabilitiesTest(_AppCase):
    """GET /capabilities。"""

    async def test_levels(self) -> None:
        app = await self.make_app()
        result = await app.capabilities("p1")
        self.assertEqual(result, {"levels": ["low", "high"], "verified": True})

    async def test_unverified(self) -> None:
        app = await self.make_app()
        app.gateway.efforts_value = None
        result = await app.capabilities("p1")
        self.assertEqual(result, {"levels": [], "verified": False})

    async def test_invalid_provider_id(self) -> None:
        app = await self.make_app()
        for value in (None, "", "x" * 513):
            with self.assertRaises(ConsoleError) as caught:
                await app.capabilities(value)
            self.assertEqual(caught.exception.code, "INVALID_PROVIDER_ID")


class BackupsTest(_AppCase):
    """GET /backups 与告警。"""

    async def test_local_backup_list(self) -> None:
        app = await self.make_app({"backup_keep": 5, "backup_max_mb": 10})
        engine = FakeEngine.instance
        engine.backups = [
            {
                "id": BACKUP_UUID,
                "createdAt": "2026-01-01T00:00:00.000Z",
                "bytes": 1024.7,
                "files": 3,
                "sqlite": "ok",
            },
            {"id": "/abs/path/not-a-uuid", "bytes": 1},
            {"id": OTHER_UUID, "files": 0, "sqlite": "Invalid!"},
        ]
        engine.stats = {"count": 2, "bytes": 1024.9, "items": []}

        result = await app.backups(None)

        self.assertEqual(
            [item["id"] for item in result["backups"]], [BACKUP_UUID, OTHER_UUID]
        )
        self.assertEqual(result["backups"][0]["bytes"], 1024)
        self.assertEqual(result["backups"][1]["files"], 0)
        self.assertNotIn("sqlite", result["backups"][1])
        self.assertEqual(result["stats"], {"count": 2, "bytes": 1024})
        self.assertEqual(result["limits"], {"keep": 5, "maxBytes": 10 * 1024 * 1024})
        self.assertIsInstance(result["warnings"], list)

    async def test_backup_warnings_from_diagnostics(self) -> None:
        app = await self.make_app()
        diagnostics.log_diagnostic(
            app.state_dir,
            "warn",
            "backup-warning",
            {"code": "BACKUP_SIZE_LIMIT_EXCEEDED", "detail": "maxBytes=1"},
        )
        diagnostics.log_diagnostic(
            app.state_dir, "info", "unrelated", {"code": "NOT_A_BACKUP_CODE"}
        )
        result = await app.backups(None)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "BACKUP_SIZE_LIMIT_EXCEEDED")
        self.assertEqual(result["warnings"][0]["message"], "maxBytes=1")

    async def test_remote_backup_requires_agent(self) -> None:
        app = await self.make_app()

        async def find_device(device_id, *, registry_dir=None):
            return {"id": "d1", "name": "远程", "kind": "http", "readOnly": True}

        with patch.object(app_module.remote_module, "find_device", find_device):
            with self.assertRaises(ConsoleError) as caught:
                await app.backups("d1")
        self.assertEqual(caught.exception.code, "DEVICE_READ_ONLY")


class BackupDiffTest(_AppCase):
    """GET /backups/<id>/diff 的参数校验与白名单。"""

    async def test_valid_diff(self) -> None:
        app = await self.make_app()
        result = await app.backup_diff(
            BACKUP_UUID, {"kind": "profile", "id": "default"}
        )
        self.assertEqual(result["backupId"], BACKUP_UUID)
        self.assertEqual(result["resource"], {"kind": "profile", "id": "default"})
        self.assertEqual(result["currentRevision"], "current-rev")
        change = result["changes"][0]
        self.assertEqual(
            set(change), {"path", "before", "value", "capability"}
        )
        self.assertNotIn("secret", change)

    async def test_invalid_backup_id(self) -> None:
        app = await self.make_app()
        for value in ("../etc", "not-a-uuid", None):
            with self.assertRaises(ConsoleError) as caught:
                await app.backup_diff(value, {"kind": "profile", "id": "default"})
            self.assertEqual(caught.exception.code, "INVALID_BACKUP_ID")

    async def test_invalid_resource(self) -> None:
        app = await self.make_app()
        for query in (
            {"kind": "bogus", "id": "x"},
            {"kind": "profile", "id": ""},
            {"kind": "provider", "id": "../evil"},
        ):
            with self.assertRaises(ConsoleError) as caught:
                await app.backup_diff(BACKUP_UUID, query)
            self.assertEqual(caught.exception.code, "INVALID_RESOURCE")

    async def test_short_backup_id_accepted(self) -> None:
        app = await self.make_app()
        result = await app.backup_diff("11111111", {"kind": "profile", "id": "default"})
        self.assertEqual(result["backupId"], "11111111")


class PruneTest(_AppCase):
    """POST /backups/prune。"""

    async def test_invalid_keep(self) -> None:
        app = await self.make_app()
        for value in (0, 101, "3", True):
            with self.assertRaises(ConsoleError) as caught:
                await app.prune_backups({"keep": value})
            self.assertEqual(caught.exception.code, "INVALID_KEEP")

    async def test_prune_empty_state(self) -> None:
        app = await self.make_app()
        result = await app.prune_backups({})
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["stats"], {"count": 0, "bytes": 0})

    async def test_prune_removes_old_entries(self) -> None:
        app = await self.make_app()
        data = self.root / "data"
        first = await backup_module.ensure_backup(data, app.state_dir, "test")
        import asyncio

        await asyncio.sleep(0.02)
        await backup_module.ensure_backup(data, app.state_dir, "test")
        result = await app.prune_backups({"keep": 1})
        self.assertEqual(result["removed"], [first["id"]])
        self.assertEqual(result["stats"]["count"], 1)


class RestorePlanTest(_AppCase):
    """POST /restore-plans。"""

    async def test_valid(self) -> None:
        app = await self.make_app()
        result = await app.restore_plan(
            {"backupId": BACKUP_UUID, "resource": {"kind": "profile", "id": "default"}}
        )
        self.assertEqual(result["mode"], "restore")
        self.assertEqual(result["backupId"], BACKUP_UUID)
        self.assertIn("changes", result)
        self.assertNotIn("after", result)

    async def test_invalid_inputs(self) -> None:
        app = await self.make_app()
        cases = [
            ({"backupId": "nope", "resource": {"kind": "profile", "id": "default"}}, "INVALID_BACKUP_ID"),
            ({"backupId": BACKUP_UUID, "resource": {"kind": "bogus", "id": "x"}}, "INVALID_RESOURCE"),
            (
                {
                    "backupId": BACKUP_UUID,
                    "resource": {"kind": "profile", "id": "default"},
                    "expectedRevision": 5,
                },
                "INVALID_REVISION",
            ),
            (
                {
                    "backupId": BACKUP_UUID,
                    "resource": {"kind": "profile", "id": "default"},
                    "expectedRevision": "x" * 129,
                },
                "INVALID_REVISION",
            ),
        ]
        for body, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ConsoleError) as caught:
                    await app.restore_plan(body)
                self.assertEqual(caught.exception.code, code)


class PlansApplyTest(_AppCase):
    """POST /plans 与 /apply。"""

    async def test_local_plan_passes_expected_revision(self) -> None:
        app = await self.make_app()
        result = await app.plans(
            {
                "resource": {"kind": "profile", "id": "default"},
                "changes": [{"path": "provider_settings.web_search", "value": "false"}],
                "expectedRevision": "rev-1",
            }
        )
        self.assertEqual(result["id"], "44444444-4444-4444-4444-444444444444")
        call = FakeEngine.instance.calls[-1]
        self.assertEqual(call[0], "plan")
        self.assertEqual(call[3], False)
        self.assertEqual(call[4], "rev-1")

    async def test_local_apply_sanitizes_backup_id(self) -> None:
        app = await self.make_app()
        result = await app.apply({"id": "plan-1"})
        self.assertEqual(result["backupId"], BACKUP_UUID)
        self.assertEqual(result["status"], "saved-and-readback-verified")

    async def test_remote_plan_requires_writable_agent(self) -> None:
        app = await self.make_app()

        async def find_device(device_id, *, registry_dir=None):
            return {"id": "d1", "kind": "http", "readOnly": True}

        with patch.object(app_module.remote_module, "find_device", find_device):
            with self.assertRaises(ConsoleError) as caught:
                await app.plans({}, "d1")
        self.assertEqual(caught.exception.code, "DEVICE_READ_ONLY")

    async def test_remote_plan_uses_remote_module(self) -> None:
        app = await self.make_app()

        async def find_device(device_id, *, registry_dir=None):
            return {"id": "d1", "kind": "agent", "readOnly": False}

        async def remote_agent_plan(device, body, *, registry_dir=None):
            return {"id": "remote-plan", "device": device["id"], "body": body}

        with patch.object(app_module.remote_module, "find_device", find_device), patch.object(
            app_module.remote_module, "remote_agent_plan", remote_agent_plan
        ):
            result = await app.plans({"resource": {"kind": "profile", "id": "default"}}, "d1")
        self.assertEqual(result["id"], "remote-plan")
        self.assertEqual(result["device"], "d1")


class DevicesTest(_AppCase):
    """GET /devices、健康检查与添加。"""

    async def test_local_only_without_registry_module(self) -> None:
        app = await self.make_app()
        result = await app.devices()
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["devices"][0]["id"], "local")
        self.assertEqual(result["devices"][0]["kind"], "local")

    async def test_devices_disabled(self) -> None:
        app = await self.make_app({"devices_enabled": False})

        async def list_devices(registry_dir):
            return [{"id": "d1"}]

        with patch.object(app_module.devices_module, "list_devices", list_devices), patch.object(
            app_module.devices_module, "public_device", lambda device: device
        ):
            result = await app.devices()
        self.assertEqual([item["id"] for item in result["devices"]], ["local"])

    async def test_registered_devices_listed(self) -> None:
        app = await self.make_app()

        async def list_devices(registry_dir):
            return [{"id": "d1", "name": "远程", "kind": "http", "readOnly": True}]

        with patch.object(app_module.devices_module, "list_devices", list_devices), patch.object(
            app_module.devices_module, "public_device", lambda device: device
        ):
            result = await app.devices()
        self.assertEqual([item["id"] for item in result["devices"]], ["local", "d1"])

    async def test_device_health_local(self) -> None:
        app = await self.make_app()

        async def find_device(device_id, *, registry_dir=None):
            return {"id": "local", "kind": "local"}

        with patch.object(app_module.remote_module, "find_device", find_device):
            self.assertEqual(await app.device_health("local"), {"reachable": True})

    async def test_device_health_whitelists_fields(self) -> None:
        app = await self.make_app()

        async def find_device(device_id, *, registry_dir=None):
            return {"id": "d1", "kind": "http"}

        async def remote_health(device, *, registry_dir=None):
            return {
                "reachable": True,
                "latencyMs": 12.7,
                "error": "DEVICE_TIMEOUT",
                "secret": "leak",
            }

        with patch.object(app_module.remote_module, "find_device", find_device), patch.object(
            app_module.remote_module, "remote_health", remote_health
        ):
            result = await app.device_health("d1")
        self.assertEqual(result, {"reachable": True, "latencyMs": 12, "error": "DEVICE_TIMEOUT"})

    async def test_device_health_unreachable_default(self) -> None:
        app = await self.make_app()

        async def find_device(device_id, *, registry_dir=None):
            return {"id": "d1", "kind": "http"}

        async def remote_health(device, *, registry_dir=None):
            return {"reachable": False}

        with patch.object(app_module.remote_module, "find_device", find_device), patch.object(
            app_module.remote_module, "remote_health", remote_health
        ):
            result = await app.device_health("d1")
        self.assertEqual(result, {"reachable": False, "error": "DEVICE_UNREACHABLE"})

    async def test_unknown_device_health(self) -> None:
        app = await self.make_app()
        with self.assertRaises(ConsoleError) as caught:
            await app.device_health("ghost")
        self.assertEqual(caught.exception.code, "DEVICE_NOT_FOUND")

    async def test_delete_local_rejected(self) -> None:
        app = await self.make_app()
        with self.assertRaises(ConsoleError) as caught:
            await app.delete_device("local")
        self.assertEqual(caught.exception.code, "LOCAL_DEVICE_READONLY")


class SearchTestTest(_AppCase):
    """搜索链路测试端点。"""

    async def test_disabled(self) -> None:
        app = await self.make_app({"search_test_enabled": False})
        for factory in (
            lambda: app.search_key_test({"provider": "tavily", "key": "k"}),
            lambda: app.search_native_test({"profileId": "default"}),
        ):
            with self.assertRaises(ConsoleError) as caught:
                await factory()
            self.assertEqual(caught.exception.code, "SEARCH_TEST_DISABLED")

    async def test_key_test_invalid_provider(self) -> None:
        app = await self.make_app()
        with patch.object(app_module.search_key_module, "SUPPORTED_PROVIDERS", ["tavily"]):
            with self.assertRaises(ConsoleError) as caught:
                await app.search_key_test({"provider": "nope", "key": "k"})
        self.assertEqual(caught.exception.code, "INVALID_PROVIDER")

    async def test_key_required(self) -> None:
        app = await self.make_app()
        with patch.object(app_module.search_key_module, "SUPPORTED_PROVIDERS", ["tavily"]):
            with self.assertRaises(ConsoleError) as caught:
                await app.search_key_test({"provider": "tavily"})
        self.assertEqual(caught.exception.code, "KEY_REQUIRED")

    async def test_profile_unavailable(self) -> None:
        app = await self.make_app()
        app.gateway.profiles = {}
        with patch.object(app_module.search_key_module, "SUPPORTED_PROVIDERS", ["tavily"]):
            with self.assertRaises(ConsoleError) as caught:
                await app.search_key_test({"provider": "tavily", "profileId": "missing"})
        self.assertEqual(caught.exception.code, "PROFILE_UNAVAILABLE")

    async def test_missing_saved_key_message(self) -> None:
        app = await self.make_app()
        app.gateway.profiles = {"default": {"provider_settings": {}}}
        with patch.object(app_module.search_key_module, "SUPPORTED_PROVIDERS", ["tavily"]):
            result = await app.search_key_test({"provider": "tavily", "profileId": "default"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], app_module.KEY_MISSING_MESSAGE)
        self.assertEqual(result["provider"], "tavily")

    async def test_key_from_profile_and_rate_limit(self) -> None:
        app = await self.make_app()
        app.gateway.profiles = {
            "default": {"provider_settings": {"websearch_tavily_key": ["sk-1", "sk-2"]}}
        }
        calls: list[tuple] = []

        async def fake_test(provider: str, key: str) -> dict:
            calls.append((provider, key))
            return {"ok": True, "provider": provider, "label": "Tavily", "status": 200, "latencyMs": 5}

        with patch.object(app_module.search_key_module, "SUPPORTED_PROVIDERS", ["tavily"]), patch.object(
            app_module.search_key_module, "test_search_key", fake_test
        ):
            result = await app.search_key_test(
                {"provider": "tavily", "profileId": "default"}
            )
            self.assertTrue(result["ok"])
            self.assertEqual(calls[-1], ("tavily", "sk-1"))
            for _ in range(app_module.KEY_TEST_LIMIT - 1):
                await app.search_key_test({"provider": "tavily", "key": "k"})
            with self.assertRaises(ConsoleError) as caught:
                await app.search_key_test({"provider": "tavily", "key": "k"})
        self.assertEqual(caught.exception.code, "TEST_RATE_LIMITED")

    async def test_key_too_long(self) -> None:
        app = await self.make_app()
        with patch.object(app_module.search_key_module, "SUPPORTED_PROVIDERS", ["tavily"]):
            with self.assertRaises(ConsoleError) as caught:
                await app.search_key_test({"provider": "tavily", "key": "k" * 4097})
        self.assertEqual(caught.exception.code, "KEY_TOO_LONG")

    async def test_native_test_success_and_limits(self) -> None:
        app = await self.make_app()
        app.gateway.profiles = {
            "default": {
                "provider_settings": {"websearch_native_provider_id": "p1"},
                "agent_runner": {"config": {"model": {"provider_id": "p2"}}},
            }
        }
        calls: list[tuple] = []

        async def fake_native(config: dict, provider_id: str) -> dict:
            calls.append((config, provider_id))
            return {"ok": True, "providerId": provider_id}

        with patch.object(app_module.native_search_module, "test_native_search", fake_native):
            result = await app.search_native_test({"profileId": "default"})
            self.assertEqual(result["providerId"], "p1")
            self.assertEqual(calls[-1][1], "p1")
            for _ in range(app_module.NATIVE_TEST_LIMIT - 1):
                await app.search_native_test({"profileId": "default", "providerId": "px"})
            with self.assertRaises(ConsoleError) as caught:
                await app.search_native_test({"profileId": "default"})
        self.assertEqual(caught.exception.code, "TEST_RATE_LIMITED")

    async def test_native_test_requires_profile_and_provider(self) -> None:
        app = await self.make_app()
        with self.assertRaises(ConsoleError) as caught:
            await app.search_native_test({})
        self.assertEqual(caught.exception.code, "PROFILE_REQUIRED")
        app.gateway.profiles = {"default": {}}
        with self.assertRaises(ConsoleError) as caught:
            await app.search_native_test({"profileId": "default"})
        self.assertEqual(caught.exception.code, "PROVIDER_REQUIRED")


class LegacyImportStickyTest(_AppCase):
    """legacyImported 必须跨插件重载保持 true（live 实测缺陷的回归测试）。"""

    async def test_sticky_across_reload(self) -> None:
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "devices.json").write_text('{"devices":[]}', encoding="utf-8")
        (legacy / "audit.jsonl").write_text("{}\n", encoding="utf-8")
        with patch.object(app_module.paths, "legacy_state_dir", lambda: legacy):
            first = await self.make_app()
            state1 = await first.state()
            self.assertTrue(state1["legacyImported"])
            self.assertTrue(state1["legacyStatePresent"])
            self.assertEqual(state1["legacyStateDir"], str(legacy))
            record1 = state1["legacyImport"]
            self.assertEqual(record1["status"], "imported")
            self.assertEqual(record1["copied"], 2)
            self.assertEqual(record1["files"], 2)
            self.assertTrue((self.root / "state" / "legacy-import.json").is_file())

            # 模拟插件重载：新的 ConsoleApp、同一个状态目录，旧目录已被导入过。
            second = await self.make_app()
            state2 = await second.state()
            self.assertTrue(state2["legacyImported"])
            record2 = state2["legacyImport"]
            self.assertEqual(record2["copied"], 0)
            self.assertEqual(record2["skipped"], 2)
            self.assertEqual(record2["status"], "unchanged")
            self.assertTrue(record2["imported"])
            self.assertEqual(record2["firstImportedAt"], record1["firstImportedAt"])

            # 旧目录后来被删掉，但历史记录仍在 → 依旧算「导入过」。
            with patch.object(app_module.paths, "legacy_state_dir", lambda: None):
                third = await self.make_app()
                state3 = await third.state()
        self.assertTrue(state3["legacyImported"])
        self.assertFalse(state3["legacyStatePresent"])
        self.assertTrue(state3["legacyImport"]["imported"])

    async def test_no_legacy_no_record(self) -> None:
        with patch.object(app_module.paths, "legacy_state_dir", lambda: None):
            app = await self.make_app()
            state = await app.state()
        self.assertFalse(state["legacyImported"])
        self.assertIsNone(state["legacyImport"])
        self.assertFalse(state["legacyStatePresent"])
        self.assertIsNone(state["legacyStateDir"])

    async def test_empty_legacy_dir_is_not_imported(self) -> None:
        legacy = self.root / "empty-legacy"
        legacy.mkdir()
        with patch.object(app_module.paths, "legacy_state_dir", lambda: legacy):
            app = await self.make_app()
            state = await app.state()
        self.assertFalse(state["legacyImported"])
        self.assertFalse(state["legacyStatePresent"])
        self.assertEqual(state["legacyImport"]["status"], "unchanged")

    async def test_imported_when_files_already_present_without_record(self) -> None:
        """上一版插件导入过、但没有记录文件：copied=0/skipped>0 也必须算「已导入」。"""
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "devices.json").write_text('{"devices":[]}', encoding="utf-8")
        (legacy / "audit.jsonl").write_text("{}\n", encoding="utf-8")
        state = self.root / "state"
        state.mkdir()
        # 模拟上一版插件：文件已复制到目标目录，但没有 legacy-import.json。
        (state / "devices.json").write_text('{"devices":[]}', encoding="utf-8")
        (state / "audit.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertFalse((state / "legacy-import.json").exists())
        with patch.object(app_module.paths, "legacy_state_dir", lambda: legacy):
            app = await self.make_app()
            view = await app.state()
        self.assertTrue(view["legacyImported"])
        self.assertTrue(view["legacyStatePresent"])
        record = view["legacyImport"]
        self.assertEqual(record["copied"], 0)
        self.assertEqual(record["skipped"], 2)
        self.assertEqual(record["status"], "unchanged")
        self.assertTrue(record["imported"])
        self.assertTrue((state / "legacy-import.json").is_file())

    async def test_migrate_keeps_sticky_flag(self) -> None:
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "devices.json").write_text("{}", encoding="utf-8")
        with patch.object(app_module.paths, "legacy_state_dir", lambda: legacy):
            app = await self.make_app()
            result = await app.migrate_state({})
            state = await app.state()
        self.assertFalse(result["imported"])  # 本次没有新文件
        self.assertTrue(result["legacyImported"])
        self.assertTrue(result["legacyStatePresent"])
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(state["legacyImported"])


class MigrateTest(_AppCase):
    """POST /state/migrate。"""

    async def test_migrate_imports_legacy(self) -> None:
        app = await self.make_app()
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "devices.json").write_text('{"devices":[]}', encoding="utf-8")
        with patch.object(
            app_module.paths, "legacy_state_dir", lambda: legacy
        ):
            result = await app.migrate_state({})
        self.assertTrue(result["imported"])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["legacyStateDir"], str(legacy))
        self.assertTrue((app.state_dir / "devices.json").is_file())

    async def test_migrate_without_legacy(self) -> None:
        app = await self.make_app()
        with patch.object(app_module.paths, "legacy_state_dir", lambda: None):
            result = await app.migrate_state({})
        self.assertFalse(result["imported"])
        self.assertEqual(result["files"], 0)
        self.assertIsNone(result["legacyStateDir"])


class DiagnosticsExportTest(_AppCase):
    """GET /diagnostics/export。"""

    async def test_export(self) -> None:
        app = await self.make_app()
        diagnostics.log_diagnostic(
            app.state_dir, "info", "request-rejected", {"detail": "nope"}
        )
        result = await app.diagnostics_export()
        self.assertGreaterEqual(len(result["events"]), 1)
        self.assertIn("event", result["events"][-1])


class RegistryIntegrationTest(_AppCase):
    """与真实 console.devices 模块的集成（不联网）。"""

    async def test_list_and_delete_registered_device(self) -> None:
        app = await self.make_app()
        (app.state_dir / "devices.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "id": "dev-1",
                            "name": "笔记本",
                            "kind": "http",
                            "baseUrl": "http://127.0.0.1:6185",
                            "readOnly": True,
                            "auth": {"type": "api_key"},
                            "createdAt": "2026-01-01T00:00:00.000Z",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = await app.devices()
        self.assertEqual([item["id"] for item in result["devices"]], ["local", "dev-1"])
        entry = result["devices"][1]
        self.assertNotIn("auth", entry)
        self.assertNotIn("apiKey", entry)
        self.assertEqual(entry["baseUrl"], "http://127.0.0.1:6185")

        with self.assertRaises(ConsoleError) as caught:
            await app.device_health("ghost")
        self.assertEqual(caught.exception.code, "DEVICE_NOT_FOUND")

        self.assertEqual(await app.delete_device("dev-1"), {"removed": True})
        after = await app.devices()
        self.assertEqual([item["id"] for item in after["devices"]], ["local"])

    @unittest.skipUnless(os.name == "nt", "DPAPI 凭据保护仅在 Windows 可用")
    async def test_add_device_with_real_registry(self) -> None:
        app = await self.make_app()
        created = await app.add_device(
            {
                "name": "笔记本",
                "kind": "http",
                "baseUrl": "http://127.0.0.1:6185",
                "apiKey": "abk_test",
            }
        )
        device = created["device"]
        self.assertTrue(device["hasSecret"])
        self.assertNotIn("apiKey", device)
        listed = await app.devices()
        self.assertIn(device["id"], [item["id"] for item in listed["devices"]])
        removed = await app.delete_device(device["id"])
        self.assertEqual(removed, {"removed": True})

    async def test_add_http_device_requires_key(self) -> None:
        app = await self.make_app()
        with self.assertRaises(ConsoleError) as caught:
            await app.add_device({"name": "x", "kind": "http", "baseUrl": "http://127.0.0.1:9999"})
        self.assertEqual(caught.exception.code, "DEVICE_SECRET_REQUIRED")


class HelpersTest(unittest.TestCase):
    """模块级白名单/校验函数的纯函数行为。"""

    def test_backup_id_of(self) -> None:
        self.assertEqual(backup_id_of(BACKUP_UUID), BACKUP_UUID)
        self.assertEqual(backup_id_of(f"backups/{BACKUP_UUID}"), BACKUP_UUID)
        self.assertEqual(backup_id_of("11111111"), "11111111")
        self.assertIsNone(backup_id_of("../etc"))
        self.assertIsNone(backup_id_of(123))

    def test_parse_resource(self) -> None:
        self.assertEqual(
            parse_resource({"kind": "profile", "id": "default"}),
            {"kind": "profile", "id": "default"},
        )
        self.assertEqual(
            parse_resource({"kind": "provider", "id": "example/model-a"}),
            {"kind": "provider", "id": "example/model-a"},
        )
        self.assertIsNone(parse_resource({"kind": "provider", "id": "../x"}))
        self.assertIsNone(parse_resource({"kind": "bogus", "id": "x"}))
        self.assertIsNone(parse_resource(None))

    def test_public_health(self) -> None:
        self.assertEqual(public_health({"reachable": True, "latencyMs": 3.9}), {"reachable": True, "latencyMs": 3})
        self.assertEqual(public_health({"reachable": False}), {"reachable": False, "error": "DEVICE_UNREACHABLE"})
        self.assertEqual(
            public_health({"reachable": False, "error": "bad-code"}),
            {"reachable": False, "error": "DEVICE_UNREACHABLE"},
        )

    def test_public_backup(self) -> None:
        entry = public_backup(
            {
                "id": f"backups/{BACKUP_UUID}",
                "createdAt": "2026-01-01T00:00:00.000Z",
                "bytes": 10.9,
                "files": 2,
                "sqlite": "ok",
                "extra": "drop",
            }
        )
        assert entry is not None
        self.assertEqual(entry["id"], BACKUP_UUID)
        self.assertEqual(entry["bytes"], 10)
        self.assertNotIn("extra", entry)
        self.assertIsNone(public_backup({"id": "/tmp/x"}))

    def test_public_changes(self) -> None:
        changes = public_changes(
            [{"path": "a", "before": 1, "value": 2, "capability": "llm", "secret": True}, None]
        )
        self.assertEqual(changes, [{"path": "a", "before": 1, "value": 2, "capability": "llm"}])
        self.assertEqual(public_changes(None), [])

    def test_public_restore_plan(self) -> None:
        view = public_restore_plan(
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "resource": {"kind": "profile", "id": "default"},
                "revision": "rev",
                "expiresAt": "2026-01-01T00:10:00.000Z",
                "impact": "this profile",
                "warning": "warning",
                "after": {"leak": True},
                "backupId": BACKUP_UUID,
            }
        )
        self.assertEqual(view["mode"], "restore")
        self.assertEqual(view["id"], "33333333-3333-3333-3333-333333333333")
        self.assertNotIn("after", view)
        self.assertNotIn("backupId", view)

    def test_safe_apply_result(self) -> None:
        result = safe_apply_result(
            {"status": "ok", "backupId": f"E:\\state\\backups\\{BACKUP_UUID}"}
        )
        self.assertEqual(result["backupId"], BACKUP_UUID)
        dropped = safe_apply_result({"status": "ok", "backupId": "not-a-uuid"})
        self.assertNotIn("backupId", dropped)
        self.assertEqual(safe_apply_result(None), None)

    def test_normalize_stats(self) -> None:
        self.assertEqual(normalize_stats({"count": 2, "bytes": 3.9}), {"count": 2, "bytes": 3})
        self.assertEqual(normalize_stats(None), {"count": 0, "bytes": 0})
        self.assertEqual(normalize_stats({"count": True, "bytes": -1}), {"count": 0, "bytes": 0})


if __name__ == "__main__":
    unittest.main()
