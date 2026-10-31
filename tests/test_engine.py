"""console.engine 的单元测试：快照、计划校验、revision 校验、备份阻断、回读验证与恢复。"""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from console import backup as backup_module
from console.engine import (
    Engine,
    binding_rows,
    display_changes,
    infer_capability,
    parse_secret_list,
    restore_changes,
    revision,
)
from console.errors import ConsoleError

PROFILE_CONFIG: dict = {
    "config_version": "1.0",
    "provider": [
        {
            "id": "p1",
            "model": "m1",
            "type": "openai_chat_completion",
            "enable": True,
            "custom_extra_body": {"reasoning_effort": "low"},
        },
        {"id": "p2", "model": "m2", "type": "embedding", "enable": True},
        {"id": "p3", "model": "m3", "type": "openai_chat_completion", "enable": False},
    ],
    "provider_settings": {
        "web_search": True,
        "websearch_provider": "tavily",
        "websearch_tavily_key": ["sk-1"],
    },
    "agent_runner": {"config": {"model": {"provider_id": "p1"}}},
    "custom": {"thing_provider_id": "p1"},
}

PLUGIN_CONFIG: dict = {
    "semantic": {"embedding_provider_id": "p2"},
    "decision_ai_provider_id": "p1",
}


class FakeGateway:
    """内存版网关，实现 Engine 需要的网关形状。"""

    def __init__(
        self,
        configs: dict[tuple[str, str], dict],
        *,
        plugins: list | None = None,
        efforts: list | None = None,
    ) -> None:
        self.configs = copy.deepcopy(configs)
        self.resource_list = [
            {"kind": kind, "id": identifier} for kind, identifier in self.configs
        ]
        self.plugins_value = plugins
        self.efforts_value = ["low", "high"] if efforts is None else efforts
        self.writes: list[tuple[dict, dict]] = []
        self.reads: list[tuple[str, str]] = []
        self.skip_write = False
        self.fail_write = False

    async def read(self, resource: dict) -> dict:
        key = (resource.get("kind"), resource.get("id"))
        if key not in self.configs:
            raise ConsoleError("NOT_FOUND")
        self.reads.append(key)
        return copy.deepcopy(self.configs[key])

    async def write(self, resource: dict, payload: dict) -> None:
        if self.fail_write:
            raise ConsoleError("WRITE_FAILED")
        self.writes.append((copy.deepcopy(resource), copy.deepcopy(payload)))
        if not self.skip_write:
            self.configs[(resource.get("kind"), resource.get("id"))] = copy.deepcopy(
                payload
            )

    async def resources(self) -> list[dict]:
        return copy.deepcopy(self.resource_list)

    async def routing_summary(self) -> dict:
        return {"total": 2, "platformLevel": 1, "granular": 1}

    async def plugins(self) -> list | None:
        return copy.deepcopy(self.plugins_value)

    async def efforts(self, provider_id: str) -> list | None:
        return copy.deepcopy(self.efforts_value)


def make_data_dir(root: Path, *, plugin_config: dict | None = None) -> Path:
    """构造最小 AstrBot data 目录。"""
    data = root / "data"
    (data / "config").mkdir(parents=True, exist_ok=True)
    (data / "cmd_config.json").write_text(
        json.dumps(PROFILE_CONFIG, ensure_ascii=False), encoding="utf-8"
    )
    (data / "config" / "meme_manager_config.json").write_text(
        json.dumps(plugin_config if plugin_config is not None else PLUGIN_CONFIG),
        encoding="utf-8",
    )
    connection = sqlite3.connect(data / "data_v4.db")
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    return data


class _EngineCase(unittest.IsolatedAsyncioTestCase):
    """带临时目录与假网关的测试基类。"""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = make_data_dir(self.root)
        self.state = self.root / "state"
        self.state.mkdir()

    def make_engine(
        self,
        *,
        read_only: bool = False,
        configs: dict | None = None,
        plugins: list | None = None,
        efforts: list | None = None,
    ) -> tuple[Engine, FakeGateway]:
        """创建引擎与假网关。"""
        gateway = FakeGateway(
            configs
            if configs is not None
            else {
                ("profile", "default"): PROFILE_CONFIG,
                ("plugin", "meme_manager"): PLUGIN_CONFIG,
                ("plugin", "astrbot_plugin_group_chat_plus"): {
                    "decision_ai_provider_id": "p1",
                    "image_to_text_provider_id": "",
                    "private_image_to_text_provider_id": "",
                },
            },
            plugins=plugins,
            efforts=efforts,
        )
        engine = Engine(
            gateway,
            self.state,
            self.data,
            {"keep": 10, "maxBytes": 2048 * 1024 * 1024},
            read_only,
        )
        return engine, gateway

    def audit_events(self) -> list[dict]:
        """读取审计日志事件。"""
        path = self.state / "audit.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class SnapshotTest(_EngineCase):
    """快照结构与绑定展示。"""

    async def test_snapshot_shape_and_bindings(self) -> None:
        engine, gateway = self.make_engine(
            plugins=[
                {
                    "name": "astrbot_plugin_model_websearch",
                    "version": "v1.0.0",
                    "activated": True,
                }
            ]
        )
        snapshot = await engine.snapshot()

        self.assertEqual(
            set(snapshot),
            {
                "readAt",
                "writeEnabled",
                "configVersion",
                "coverage",
                "routing",
                "plugins",
                "features",
                "models",
                "resources",
            },
        )
        self.assertTrue(snapshot["readAt"].endswith("Z"))
        self.assertTrue(snapshot["writeEnabled"])
        self.assertEqual(snapshot["configVersion"], "1.0")
        self.assertEqual(
            snapshot["routing"], {"total": 2, "platformLevel": 1, "granular": 1}
        )
        self.assertTrue(snapshot["features"]["nativeWebSearch"]["installed"])
        self.assertTrue(snapshot["features"]["nativeWebSearch"]["activated"])

        model_ids = [model["id"] for model in snapshot["models"]]
        self.assertEqual(model_ids, ["p1", "p2", "p3"])
        self.assertEqual(snapshot["models"][0]["capabilities"], ["llm"])
        self.assertEqual(snapshot["models"][1]["capabilities"], ["embedding"])
        self.assertFalse(snapshot["models"][2]["enabled"])
        self.assertEqual(
            snapshot["models"][0]["writableFields"],
            ["custom_extra_body.reasoning_effort"],
        )

        resources = snapshot["resources"]
        self.assertEqual(
            [(item["resource"]["kind"], item["resource"]["id"]) for item in resources],
            [
                ("profile", "default"),
                ("plugin", "meme_manager"),
                ("plugin", "astrbot_plugin_group_chat_plus"),
            ],
        )
        profile = resources[0]
        self.assertEqual(profile["revision"], revision(PROFILE_CONFIG))
        binding = next(
            item
            for item in profile["bindings"]
            if item["path"] == "agent_runner.config.model.provider_id"
        )
        self.assertEqual(binding["capability"], "llm")
        self.assertEqual(binding["value"], "p1")
        self.assertEqual(binding["effectiveModel"], "p1")
        self.assertEqual(binding["status"], "configured")
        missing = next(
            item
            for item in profile["bindings"]
            if item["path"] == "provider_stt_settings.provider_id"
        )
        self.assertEqual(missing["status"], "field-missing")
        secret = next(
            item
            for item in profile["bindings"]
            if item["path"] == "provider_settings.websearch_tavily_key"
        )
        self.assertTrue(secret["configured"])
        self.assertEqual(secret["count"], 1)
        self.assertNotIn("sk-1", json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(
            [item["path"] for item in profile["unreviewed"]],
            ["custom.thing_provider_id"],
        )

    async def test_plugin_bindings_and_unreviewed(self) -> None:
        engine, _gateway = self.make_engine()
        snapshot = await engine.snapshot()
        meme = snapshot["resources"][1]
        meme_paths = [item["path"] for item in meme["bindings"]]
        self.assertIn("semantic.embedding_provider_id", meme_paths)
        self.assertIn("generation.emotion.llm.provider_id", meme_paths)
        group = snapshot["resources"][2]
        decision = next(
            item for item in group["bindings"] if item["path"] == "decision_ai_provider_id"
        )
        self.assertEqual(decision["value"], "p1")
        self.assertEqual(decision["inherit"], "session")

    async def test_snapshot_cached(self) -> None:
        engine, gateway = self.make_engine()
        first = await engine.snapshot()
        reads_after_first = len(gateway.reads)
        second = await engine.snapshot()
        self.assertEqual(first["readAt"], second["readAt"])
        self.assertEqual(len(gateway.reads), reads_after_first)

    async def test_unavailable_resource_marked(self) -> None:
        engine, gateway = self.make_engine()
        gateway.resource_list.append({"kind": "plugin", "id": "gone"})
        snapshot = await engine.snapshot()
        unavailable = snapshot["resources"][-1]
        self.assertEqual(unavailable["status"], "unavailable")

    async def test_native_search_absent_marks_plugin_unavailable(self) -> None:
        engine, _gateway = self.make_engine(plugins=[])
        snapshot = await engine.snapshot()
        binding = next(
            item
            for item in snapshot["resources"][0]["bindings"]
            if item["path"] == "provider_settings.websearch_provider"
        )
        self.assertEqual(binding["status"], "configured")
        # 无原生搜索插件时不会出现 model_native 选项
        self.assertNotIn("model_native", [option["value"] for option in binding["options"]])


class RevisionTest(unittest.TestCase):
    """revision 规范化。"""

    def test_key_order_irrelevant(self) -> None:
        self.assertEqual(revision({"a": 1, "b": 2}), revision({"b": 2, "a": 1}))

    def test_array_order_matters(self) -> None:
        self.assertNotEqual(revision([1, 2]), revision([2, 1]))

    def test_nested_normalization(self) -> None:
        left = {"outer": {"x": 1, "y": [{"b": 2, "a": 1}]}}
        right = {"outer": {"y": [{"a": 1, "b": 2}], "x": 1}}
        self.assertEqual(revision(left), revision(right))


class SecretListTest(unittest.TestCase):
    """parse_secret_list 与显示摘要。"""

    def test_json_string(self) -> None:
        self.assertEqual(parse_secret_list('[" a ", "b", ""]'), ["a", "b"])
        self.assertEqual(parse_secret_list("   "), [])
        self.assertIsNone(parse_secret_list("[oops"))
        self.assertIsNone(parse_secret_list("{}"))
        self.assertIsNone(parse_secret_list(123))

    def test_limits(self) -> None:
        self.assertIsNone(parse_secret_list(json.dumps([f"k{i}" for i in range(21)])))
        self.assertIsNone(parse_secret_list(json.dumps(["x" * 4097])))

    def test_display_masks_secret(self) -> None:
        changes = display_changes(
            [
                {"path": "provider_settings.websearch_tavily_key", "value": '["sk-2"]'},
                {"path": "provider_settings.web_search", "value": "false"},
            ]
        )
        self.assertEqual(changes[0]["value"], "（已隐藏）")
        self.assertTrue(changes[0]["secret"])
        self.assertEqual(changes[0]["summary"], "设置 1 个 Key（已隐藏）")
        self.assertEqual(changes[1]["value"], "false")
        self.assertNotIn("sk-2", json.dumps(changes, ensure_ascii=False))

    def test_display_empty_secret(self) -> None:
        changes = display_changes(
            [{"path": "provider_settings.websearch_tavily_key", "value": "[]"}]
        )
        self.assertEqual(changes[0]["summary"], "清空 Key")


class InferCapabilityTest(unittest.TestCase):
    """provider 类型 → 能力。"""

    def test_mappings(self) -> None:
        self.assertEqual(infer_capability("embedding"), ["embedding"])
        self.assertEqual(infer_capability("rerank"), ["rerank"])
        self.assertEqual(infer_capability("openai_stt"), ["stt"])
        self.assertEqual(infer_capability("openai_tts"), ["tts"])
        self.assertEqual(infer_capability("openai_chat_completion"), ["llm"])
        self.assertEqual(infer_capability("openai_responses"), ["llm"])
        self.assertEqual(infer_capability("weird"), [])
        self.assertEqual(infer_capability(None), [])


class PlanTest(_EngineCase):
    """计划生成与逐字段校验。"""

    async def test_bool_and_enum_change(self) -> None:
        engine, _gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [
                {"path": "provider_settings.web_search", "value": "false"},
                {"path": "provider_settings.websearch_provider", "value": "brave"},
            ],
        )
        self.assertRegex(plan["id"], r"^[0-9a-f-]{36}$")
        self.assertTrue(plan["expiresAt"].endswith("Z"))
        self.assertEqual(plan["impact"], "this profile")
        self.assertEqual(len(plan["changes"]), 2)
        self.assertIn(plan["id"], engine.plans)
        after = engine.plans[plan["id"]]["after"]
        self.assertIs(after["provider_settings"]["web_search"], False)
        self.assertEqual(after["provider_settings"]["websearch_provider"], "brave")

    async def test_expected_revision_mismatch(self) -> None:
        engine, _gateway = self.make_engine()
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "profile", "id": "default"},
                [{"path": "provider_settings.web_search", "value": "false"}],
                expected_revision="deadbeef",
            )
        self.assertEqual(caught.exception.code, "CONCURRENT_CHANGE")

    async def test_expected_revision_match(self) -> None:
        engine, _gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
            expected_revision=revision(PROFILE_CONFIG),
        )
        self.assertIn(plan["id"], engine.plans)

    async def test_invalid_plans(self) -> None:
        engine, _gateway = self.make_engine()
        cases = [
            ({"kind": "profile", "id": "default"}, []),
            ({"kind": "bogus", "id": "default"}, [{"path": "x", "value": "y"}]),
            ({"kind": "profile", "id": 5}, [{"path": "x", "value": "y"}]),
            (
                {"kind": "profile", "id": "default"},
                [{"path": f"p{i}", "value": "x"} for i in range(31)],
            ),
        ]
        for resource, changes in cases:
            with self.subTest(resource=resource, count=len(changes)):
                with self.assertRaises(ConsoleError) as caught:
                    await engine.plan(resource, changes)
                self.assertEqual(caught.exception.code, "INVALID_PLAN")

    async def test_duplicate_and_unknown_field(self) -> None:
        engine, _gateway = self.make_engine()
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "profile", "id": "default"},
                [
                    {"path": "provider_settings.web_search", "value": "false"},
                    {"path": "provider_settings.web_search", "value": "true"},
                ],
            )
        self.assertEqual(caught.exception.code, "INVALID_CHANGE")
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "profile", "id": "default"},
                [{"path": "not.a.field", "value": "x"}],
            )
        self.assertEqual(caught.exception.code, "FIELD_NOT_WRITABLE")
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan({"kind": "profile", "id": "default"}, [{}])
        self.assertEqual(caught.exception.code, "INVALID_CHANGE")

    async def test_value_validation(self) -> None:
        engine, _gateway = self.make_engine()
        cases = [
            ("provider_settings.web_search", "yes", "VALUE_NOT_ALLOWED"),
            ("provider_settings.websearch_provider", "nope", "VALUE_NOT_ALLOWED"),
            ("provider_settings.websearch_tavily_key", "not-json", "SECRET_VALUE_INVALID"),
            ("provider_settings.websearch_tavily_key", [f"k{i}" for i in range(21)], "SECRET_VALUE_INVALID"),
            ("provider_settings.websearch_native_provider_id", "missing", "MODEL_MISSING_OR_DISABLED"),
            ("provider_settings.websearch_native_provider_id", "p3", "MODEL_MISSING_OR_DISABLED"),
            ("provider_settings.websearch_native_provider_id", "p2", "CAPABILITY_UNVERIFIED_OR_MISMATCH"),
            ("agent_runner.config.compression.provider_id", "missing", "MODEL_MISSING_OR_DISABLED"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                with self.assertRaises(ConsoleError) as caught:
                    await engine.plan(
                        {"kind": "profile", "id": "default"},
                        [{"path": path, "value": value}],
                    )
                self.assertEqual(caught.exception.code, expected)

    async def test_embedding_requires_separate_workflow(self) -> None:
        engine, _gateway = self.make_engine()
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "plugin", "id": "meme_manager"},
                [{"path": "semantic.embedding_provider_id", "value": "p2"}],
            )
        self.assertEqual(
            caught.exception.code, "INDEX_MIGRATION_REQUIRES_SEPARATE_WORKFLOW"
        )

    async def test_native_search_plugin_unavailable(self) -> None:
        engine, _gateway = self.make_engine(
            plugins=[
                {
                    "name": "astrbot_plugin_model_websearch",
                    "version": "v1",
                    "activated": False,
                }
            ]
        )
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "profile", "id": "default"},
                [
                    {
                        "path": "provider_settings.websearch_provider",
                        "value": "model_native",
                    }
                ],
            )
        self.assertEqual(caught.exception.code, "NATIVE_SEARCH_PLUGIN_UNAVAILABLE")

    async def test_provider_effort_plan(self) -> None:
        provider = PROFILE_CONFIG["provider"][0]
        engine, _gateway = self.make_engine(
            configs={
                ("profile", "default"): PROFILE_CONFIG,
                ("provider", "p1"): provider,
            }
        )
        plan = await engine.plan(
            {"kind": "provider", "id": "p1"},
            [{"path": "custom_extra_body.reasoning_effort", "value": "high"}],
        )
        self.assertEqual(plan["impact"], "all references to this shared model")
        self.assertEqual(plan["changes"][0]["path"], "custom_extra_body.reasoning_effort")

    async def test_provider_effort_rejected(self) -> None:
        provider = PROFILE_CONFIG["provider"][0]
        engine, _gateway = self.make_engine(
            configs={
                ("profile", "default"): PROFILE_CONFIG,
                ("provider", "p1"): provider,
            }
        )
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "provider", "id": "p1"},
                [{"path": "custom_extra_body.reasoning_effort", "value": "ultra"}],
            )
        self.assertEqual(
            caught.exception.code, "EFFORT_UNVERIFIED_OR_UNSUPPORTED"
        )
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "provider", "id": "p1"},
                [{"path": "model", "value": "m2"}],
            )
        self.assertEqual(caught.exception.code, "FIELD_NOT_WRITABLE")

    async def test_plan_limit(self) -> None:
        engine, _gateway = self.make_engine()
        engine.plans = {
            f"id-{index}": {"expires": 10**18} for index in range(100)
        }
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan(
                {"kind": "profile", "id": "default"},
                [{"path": "provider_settings.web_search", "value": "false"}],
            )
        self.assertEqual(caught.exception.code, "PLAN_LIMIT")


class ApplyTest(_EngineCase):
    """应用链路：备份 → 写入 → 回读 → 审计。"""

    async def test_apply_success(self) -> None:
        engine, gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
        )
        result = await engine.apply(plan["id"])

        self.assertEqual(result["status"], "saved-and-readback-verified")
        self.assertEqual(result["id"], plan["id"])
        self.assertRegex(result["backupId"], r"^[0-9a-f-]{36}$")
        self.assertFalse(result["generationVerified"])
        self.assertFalse(result["hotLoadVerified"])
        self.assertEqual(len(gateway.writes), 1)
        self.assertIs(
            gateway.configs[("profile", "default")]["provider_settings"]["web_search"],
            False,
        )
        self.assertNotIn(plan["id"], engine.plans)
        events = [item["event"] for item in self.audit_events()]
        self.assertEqual(events, ["apply-start", "applied"])
        backup_dir = self.state / "backups" / result["backupId"]
        self.assertTrue((backup_dir / "manifest.json").is_file())

    async def test_apply_concurrent_change(self) -> None:
        engine, gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
        )
        gateway.configs[("profile", "default")]["config_version"] = "changed"
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply(plan["id"])
        self.assertEqual(caught.exception.code, "CONCURRENT_CHANGE")
        self.assertEqual(gateway.writes, [])
        events = [item["event"] for item in self.audit_events()]
        self.assertEqual(events, ["apply-failed"])

    async def test_apply_verify_failed(self) -> None:
        engine, gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
        )
        gateway.skip_write = True
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply(plan["id"])
        self.assertEqual(caught.exception.code, "VERIFY_FAILED")
        self.assertEqual(caught.exception.details["outcome"], "possibly-written")
        self.assertRegex(caught.exception.details["backupId"], r"^[0-9a-f-]{36}$")
        self.assertIn("apply-failed", [item["event"] for item in self.audit_events()])

    async def test_apply_backup_failure_blocks_write(self) -> None:
        (self.data / "data_v4.db").unlink()
        engine, gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
        )
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply(plan["id"])
        self.assertEqual(caught.exception.code, "BACKUP_SQLITE_INVALID")
        self.assertEqual(gateway.writes, [])
        self.assertEqual(caught.exception.details["outcome"], "not-written")
        events = self.audit_events()
        self.assertEqual([item["event"] for item in events], ["apply-failed"])
        self.assertEqual(events[0]["code"], "BACKUP_SQLITE_INVALID")
        self.assertNotIn(plan["id"], engine.plans)

    async def test_apply_read_only(self) -> None:
        engine, _gateway = self.make_engine(read_only=True)
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply("anything")
        self.assertEqual(caught.exception.code, "READ_ONLY")

    async def test_apply_expired_and_unknown(self) -> None:
        engine, _gateway = self.make_engine()
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply("missing")
        self.assertEqual(caught.exception.code, "PLAN_EXPIRED")
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
        )
        engine.plans[plan["id"]]["expires"] = 0
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply(plan["id"])
        self.assertEqual(caught.exception.code, "PLAN_EXPIRED")

    async def test_apply_busy(self) -> None:
        engine, _gateway = self.make_engine()
        engine._busy = True
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply("x")
        self.assertEqual(caught.exception.code, "WRITE_IN_PROGRESS")

    async def test_apply_write_failure_audits(self) -> None:
        engine, gateway = self.make_engine()
        plan = await engine.plan(
            {"kind": "profile", "id": "default"},
            [{"path": "provider_settings.web_search", "value": "false"}],
        )
        gateway.fail_write = True
        with self.assertRaises(ConsoleError) as caught:
            await engine.apply(plan["id"])
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        events = [item["event"] for item in self.audit_events()]
        self.assertEqual(events, ["apply-start", "apply-failed"])


class BackupViewTest(_EngineCase):
    """备份列表、差异与恢复计划。"""

    async def _seed_backup(self, engine: Engine) -> str:
        """为当前 data 目录创建一份备份，返回备份 id。"""
        created = await backup_module.ensure_backup(self.data, self.state, "test")
        return created["id"]

    async def test_backup_diff(self) -> None:
        engine, gateway = self.make_engine()
        backup_id = await self._seed_backup(engine)
        gateway.configs[("profile", "default")]["provider_settings"]["web_search"] = False

        diff = await engine.backup_diff(backup_id, {"kind": "profile", "id": "default"})
        self.assertEqual(diff["backupId"], backup_id)
        self.assertEqual(diff["resource"], {"kind": "profile", "id": "default"})
        changed = {item["path"]: item for item in diff["changes"]}
        self.assertIn("provider_settings.web_search", changed)
        self.assertEqual(changed["provider_settings.web_search"]["before"], "false")
        self.assertEqual(changed["provider_settings.web_search"]["value"], "true")
        self.assertEqual(
            diff["currentRevision"],
            revision(gateway.configs[("profile", "default")]),
        )

    async def test_plan_restore_and_apply(self) -> None:
        engine, gateway = self.make_engine()
        backup_id = await self._seed_backup(engine)
        gateway.configs[("profile", "default")]["provider_settings"]["web_search"] = False

        plan = await engine.plan_restore(
            backup_id, {"kind": "profile", "id": "default"}
        )
        self.assertEqual(plan["mode"], "restore")
        self.assertEqual(plan["backupId"], backup_id)
        changed = {item["path"]: item for item in plan["changes"]}
        self.assertEqual(changed["provider_settings.web_search"]["before"], "false")
        self.assertEqual(changed["provider_settings.web_search"]["value"], "true")
        self.assertIn(plan["id"], engine.plans)

        result = await engine.apply(plan["id"])
        self.assertEqual(result["status"], "saved-and-readback-verified")
        self.assertIs(
            gateway.configs[("profile", "default")]["provider_settings"]["web_search"],
            True,
        )

    async def test_plan_restore_expected_revision(self) -> None:
        engine, _gateway = self.make_engine()
        backup_id = await self._seed_backup(engine)
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan_restore(
                backup_id,
                {"kind": "profile", "id": "default"},
                expected_revision="nope",
            )
        self.assertEqual(caught.exception.code, "CONCURRENT_CHANGE")

    async def test_plan_restore_rejects_embedding(self) -> None:
        data = make_data_dir(
            self.root,
            plugin_config={"semantic": {"embedding_provider_id": "p2"}},
        )
        gateway = FakeGateway(
            {
                ("profile", "default"): PROFILE_CONFIG,
                ("plugin", "meme_manager"): {"semantic": {"embedding_provider_id": "p1"}},
            }
        )
        engine = Engine(gateway, self.state, data, {"keep": 10, "maxBytes": 0}, False)
        await backup_module.ensure_backup(data, self.state, "test")
        backup_id = (await engine.list_backups())[0]["id"]
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan_restore(
                backup_id, {"kind": "plugin", "id": "meme_manager"}
            )
        self.assertEqual(
            caught.exception.code, "INDEX_MIGRATION_REQUIRES_SEPARATE_WORKFLOW"
        )

    async def test_plan_restore_invalid_resource(self) -> None:
        engine, _gateway = self.make_engine()
        with self.assertRaises(ConsoleError) as caught:
            await engine.plan_restore("not-a-uuid", {"kind": "profile", "id": "default"})
        self.assertEqual(caught.exception.code, "RESTORE_BACKUP_NOT_FOUND")

    async def test_list_backups(self) -> None:
        engine, _gateway = self.make_engine()
        self.assertEqual(await engine.list_backups(), [])
        backup_id = await self._seed_backup(engine)
        listed = await engine.list_backups()
        self.assertEqual([item["id"] for item in listed], [backup_id])
        stats = await engine.backup_stats()
        self.assertEqual(stats["count"], 1)

    async def test_backup_diff_invalid_resource(self) -> None:
        engine, _gateway = self.make_engine()
        with self.assertRaises(ConsoleError) as caught:
            await engine.backup_diff("x", {"kind": "bogus", "id": "y"})
        self.assertEqual(caught.exception.code, "INVALID_RESOURCE")


class RestoreChangesTest(unittest.TestCase):
    """restore_changes 的已审查字段口径。"""

    def test_bool_and_text(self) -> None:
        changes = restore_changes(
            {"kind": "profile", "id": "default"},
            {"provider_settings": {"web_search": False}},
            {"provider_settings": {"web_search": True, "websearch_provider": "brave"}},
        )
        paths = {item["path"]: item for item in changes}
        self.assertEqual(paths["provider_settings.web_search"]["before"], "false")
        self.assertEqual(paths["provider_settings.web_search"]["value"], "true")
        self.assertEqual(paths["provider_settings.websearch_provider"]["before"], "")

    def test_provider_effort_only(self) -> None:
        changes = restore_changes(
            {"kind": "provider", "id": "p1"},
            {"custom_extra_body": {"reasoning_effort": "low"}, "model": "m1"},
            {"custom_extra_body": {"reasoning_effort": "high"}, "model": "m2"},
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["path"], "custom_extra_body.reasoning_effort")
        self.assertEqual(changes[0]["capability"], "llm")


class BindingRowsTest(unittest.TestCase):
    """binding_rows 的独立纯函数行为。"""

    def test_model_setting_options(self) -> None:
        rows = binding_rows(
            {"kind": "profile", "id": "default"},
            {"provider_settings": {"websearch_native_provider_id": "p1"}},
            [
                {"id": "p1", "enabled": True, "capabilities": ["llm"]},
                {"id": "p9", "enabled": False, "capabilities": ["llm"]},
            ],
            features=None,
        )
        row = next(
            item
            for item in rows
            if item["path"] == "provider_settings.websearch_native_provider_id"
        )
        self.assertEqual(row["value"], "p1")
        self.assertEqual(row["options"], [{"value": "p1", "label": "p1"}])
        self.assertEqual(row["status"], "configured")


if __name__ == "__main__":
    unittest.main()
