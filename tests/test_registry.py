"""console.registry 的单元测试：字段表、Secret 判定、搜索选项与未审查字段发现。"""

from __future__ import annotations

import unittest

from console import registry


class ResourceKindsTest(unittest.TestCase):
    """资源类型集合。"""

    def test_kinds(self) -> None:
        self.assertEqual(registry.RESOURCE_KINDS, frozenset({"profile", "plugin", "provider"}))


class FieldTableTest(unittest.TestCase):
    """字段表的结构与覆盖。"""

    def test_profile_table_has_core_and_settings(self) -> None:
        fields = registry.field_table("profile")
        paths = [field["path"] for field in fields]
        self.assertEqual(
            paths[:6],
            [
                "agent_runner.config.model.provider_id",
                "agent_runner.config.compression.provider_id",
                "provider_settings.default_image_caption_provider_id",
                "provider_stt_settings.provider_id",
                "provider_tts_settings.provider_id",
                "provider_ltm_settings.image_caption_provider_id",
            ],
        )
        self.assertEqual(len(fields), 18)
        capabilities = {field["capability"] for field in fields}
        self.assertIn("llm", capabilities)
        self.assertIn("setting", capabilities)

    def test_plugin_fields_by_id(self) -> None:
        self.assertEqual(len(registry.field_table("plugin", "meme_manager")), 4)
        self.assertEqual(len(registry.field_table("plugin", "astrbot_plugin_group_chat_plus")), 3)
        self.assertEqual(registry.field_table("plugin", "unknown_plugin"), [])
        self.assertEqual(registry.field_table("plugin"), [])

    def test_provider_has_no_reviewed_fields(self) -> None:
        self.assertEqual(registry.field_table("provider"), [])

    def test_returned_table_is_a_copy(self) -> None:
        first = registry.field_table("profile")
        first[0]["path"] = "mutated"
        self.assertEqual(
            registry.field_table("profile")[0]["path"],
            "agent_runner.config.model.provider_id",
        )

    def test_secret_fields_flagged(self) -> None:
        fields = {field["path"]: field for field in registry.field_table("profile")}
        tavily = fields["provider_settings.websearch_tavily_key"]
        self.assertEqual(tavily["type"], "secret-list")
        self.assertTrue(tavily["secret"])
        self.assertEqual(tavily["provider"], "tavily")
        baidu = fields["provider_settings.websearch_baidu_app_builder_key"]
        self.assertEqual(baidu["type"], "secret-string")


class SecretPathTest(unittest.TestCase):
    """is_secret_path 只认已审查的 Key 字段。"""

    def test_known_secret_paths(self) -> None:
        self.assertTrue(registry.is_secret_path("provider_settings.websearch_tavily_key"))
        self.assertTrue(registry.is_secret_path("provider_settings.websearch_serpapi_key"))
        self.assertFalse(registry.is_secret_path("provider_settings.web_search"))
        self.assertFalse(registry.is_secret_path("agent_runner.config.model.provider_id"))
        self.assertFalse(registry.is_secret_path(""))

    def test_key_field_lookup(self) -> None:
        field = registry.web_search_key_field("tavily")
        self.assertEqual(field["path"], "provider_settings.websearch_tavily_key")
        self.assertEqual(field["type"], "list")
        self.assertIsNone(registry.web_search_key_field("unknown"))
        self.assertIsNone(registry.web_search_key_field(None))


class WebSearchOptionsTest(unittest.TestCase):
    """网页搜索提供商选项与原生搜索开关。"""

    def test_builtin_order(self) -> None:
        options = registry.web_search_provider_options(None)
        self.assertEqual(len(options), 9)
        self.assertEqual(options[0], {"value": "serpapi", "label": "SerpApi"})
        self.assertEqual(options[-1]["value"], "anysearch")

    def test_native_absent_when_plugin_missing(self) -> None:
        options = registry.web_search_provider_options(
            {"nativeWebSearch": {"installed": False, "activated": False}}
        )
        self.assertEqual(len(options), 9)

    def test_native_enabled(self) -> None:
        options = registry.web_search_provider_options(
            {"nativeWebSearch": {"installed": True, "activated": True}}
        )
        self.assertEqual(len(options), 10)
        native = options[-1]
        self.assertEqual(native["value"], "model_native")
        self.assertEqual(native["label"], "跟随当前聊天模型（模型原生搜索）")
        self.assertTrue(native["native"])
        self.assertFalse(native["disabled"])

    def test_native_installed_but_disabled(self) -> None:
        options = registry.web_search_provider_options(
            {"nativeWebSearch": {"installed": True, "activated": False}}
        )
        native = options[-1]
        self.assertTrue(native["disabled"])
        self.assertIn("插件未启用", native["label"])


class PathHelpersTest(unittest.TestCase):
    """get/set/has_path 的点分路径语义。"""

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(registry.get_path({"a": {"b": 1}}, "a.c"))
        self.assertIsNone(registry.get_path(None, "a"))
        self.assertEqual(registry.get_path({"a": {"b": 1}}, "a.b"), 1)

    def test_has_path_distinguishes_absent_and_empty(self) -> None:
        config = {"a": {"b": ""}}
        self.assertTrue(registry.has_path(config, "a.b"))
        self.assertFalse(registry.has_path(config, "a.c"))
        self.assertTrue(registry.has_path({"a": None}, "a"))

    def test_set_path_builds_intermediate_objects(self) -> None:
        target: dict = {}
        registry.set_path(target, "a.b.c", 1)
        self.assertEqual(target, {"a": {"b": {"c": 1}}})
        registry.set_path(target, "a.b.d", 2)
        self.assertEqual(target["a"]["b"], {"c": 1, "d": 2})


class DiscoverTest(unittest.TestCase):
    """未审查字段发现。"""

    def test_nested_discovery(self) -> None:
        config = {
            "agent_runner": {"config": {"model": {"provider_id": "p1"}}},
            "foo": {"bar_provider_id": "p2", "nested": {"model_id": "m1"}},
            "platform": [{"provider_id": "ignored"}],
            "name": "not-a-provider",
        }
        found = registry.discover(config)
        paths = [item["path"] for item in found]
        self.assertIn("agent_runner.config.model.provider_id", paths)
        self.assertIn("foo.bar_provider_id", paths)
        self.assertIn("foo.nested.model_id", paths)
        self.assertNotIn("name", paths)
        self.assertTrue(all(item["reviewRequired"] for item in found))

    def test_build_bindings_filters_reviewed_paths(self) -> None:
        config = {
            "agent_runner": {"config": {"model": {"provider_id": "p1"}}},
            "custom": {"thing_provider_id": "p2"},
        }
        result = registry.build_bindings(config, kind="profile")
        self.assertEqual(
            [item["path"] for item in result["unreviewed"]],
            ["custom.thing_provider_id"],
        )
        self.assertEqual(len(result["discovered"]), 2)

    def test_non_mapping_input(self) -> None:
        self.assertEqual(registry.discover(None), [])
        self.assertEqual(registry.discover([{"provider_id": "x"}]), [])


if __name__ == "__main__":
    unittest.main()
