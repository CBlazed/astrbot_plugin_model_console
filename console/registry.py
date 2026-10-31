"""已审查的模型绑定语义（旧实现 ``model-console/src/registry.mjs`` 的 Python 移植）。

设计原则照旧：**只有已审查的字段才可写**。未知 schema 里发现的 ``provider_id`` / ``model_id``
一律进 ``unreviewed``，永不自动变成可写字段。

字段表结构（与旧 JSON 逐字段一致）：

* 模型绑定字段：``{path, capability, inherit}``
* 设置字段：``{path, capability: "setting", group: "websearch", type, ...}``；
  Secret 字段额外带 ``{provider, secret: true}``。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

#: 允许出现在资源引用里的类型。
RESOURCE_KINDS: frozenset[str] = frozenset({"profile", "plugin", "provider"})

#: AstrBot 配置档案的核心模型绑定字段（顺序与旧实现一致）。
_CORE_BINDINGS: list[dict[str, Any]] = [
    {"path": "agent_runner.config.model.provider_id", "capability": "llm", "inherit": None},
    {
        "path": "agent_runner.config.compression.provider_id",
        "capability": "llm",
        "inherit": None,
    },
    {
        "path": "provider_settings.default_image_caption_provider_id",
        "capability": "vision",
        "inherit": None,
    },
    {"path": "provider_stt_settings.provider_id", "capability": "stt", "inherit": None},
    {"path": "provider_tts_settings.provider_id", "capability": "tts", "inherit": None},
    {
        "path": "provider_ltm_settings.image_caption_provider_id",
        "capability": "vision",
        "inherit": None,
    },
]

#: AstrBot 内置网页搜索提供商（顺序/值与 AstrBot 4.29.3 配置项一致）。
WEB_SEARCH_PROVIDERS: list[dict[str, str]] = [
    {"value": "serpapi", "label": "SerpApi"},
    {"value": "deepseek", "label": "DeepSeek Search API"},
    {"value": "tavily", "label": "Tavily"},
    {"value": "baidu_ai_search", "label": "Baidu AI Search"},
    {"value": "bocha", "label": "Bocha"},
    {"value": "brave", "label": "Brave"},
    {"value": "firecrawl", "label": "Firecrawl"},
    {"value": "exa", "label": "Exa"},
    {"value": "anysearch", "label": "AnySearch"},
]

#: 模型原生搜索（由工作区插件提供）的固定标识。
NATIVE_SEARCH_PLUGIN: dict[str, str] = {
    "id": "astrbot_plugin_model_websearch",
    "value": "model_native",
    "label": "跟随当前聊天模型（模型原生搜索）",
}

#: 内置搜索提供商 → 保存其 API Key 的 ``provider_settings`` 字段。
_WEB_SEARCH_KEY_FIELDS: dict[str, dict[str, str]] = {
    "tavily": {"path": "provider_settings.websearch_tavily_key", "type": "list"},
    "bocha": {"path": "provider_settings.websearch_bocha_key", "type": "list"},
    "brave": {"path": "provider_settings.websearch_brave_key", "type": "list"},
    "firecrawl": {"path": "provider_settings.websearch_firecrawl_key", "type": "list"},
    "exa": {"path": "provider_settings.websearch_exa_key", "type": "list"},
    "anysearch": {"path": "provider_settings.websearch_anysearch_key", "type": "list"},
    "deepseek": {"path": "provider_settings.websearch_deepseek_key", "type": "list"},
    "serpapi": {"path": "provider_settings.websearch_serpapi_key", "type": "list"},
    "baidu_ai_search": {
        "path": "provider_settings.websearch_baidu_app_builder_key",
        "type": "string",
    },
}


def _setting(path: str, type_: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造一条设置字段定义。"""
    field: dict[str, Any] = {
        "path": path,
        "capability": "setting",
        "group": "websearch",
        "type": type_,
    }
    if extra:
        field.update(extra)
    return field


_SECRET_SETTINGS: list[dict[str, Any]] = [
    _setting(
        definition["path"],
        "secret-list" if definition["type"] == "list" else "secret-string",
        {"provider": provider, "secret": True},
    )
    for provider, definition in _WEB_SEARCH_KEY_FIELDS.items()
]

#: 档案里的网页搜索设置（含 Secret Key 字段）。
_SETTINGS: list[dict[str, Any]] = [
    _setting("provider_settings.web_search", "bool"),
    _setting("provider_settings.websearch_provider", "enum"),
    _setting("provider_settings.websearch_native_provider_id", "model"),
    *_SECRET_SETTINGS,
]

#: 已审查的插件配置绑定字段。
_PLUGIN_FIELDS: dict[str, list[dict[str, Any]]] = {
    "astrbot_plugin_group_chat_plus": [
        {"path": "decision_ai_provider_id", "capability": "llm", "inherit": "session"},
        {"path": "image_to_text_provider_id", "capability": "vision", "inherit": None},
        {
            "path": "private_image_to_text_provider_id",
            "capability": "vision",
            "inherit": None,
        },
    ],
    "astrbot_plugin_livingmemory": [
        {"path": "provider_settings.embedding_provider_id", "capability": "embedding", "inherit": None},
        {"path": "provider_settings.llm_provider_id", "capability": "llm", "inherit": None},
        {"path": "recall_engine.rerank_provider_id", "capability": "rerank", "inherit": None},
    ],
    "astrbot_plugin_self_learning": [
        {
            "path": "Model_Configuration.filter_provider_id",
            "capability": "llm",
            "inherit": None,
        },
        {
            "path": "Model_Configuration.refine_provider_id",
            "capability": "llm",
            "inherit": None,
        },
        {
            "path": "Model_Configuration.reinforce_provider_id",
            "capability": "llm",
            "inherit": None,
        },
        {
            "path": "V2_Architecture_Settings.embedding_provider_id",
            "capability": "embedding",
            "inherit": None,
        },
        {
            "path": "V2_Architecture_Settings.rerank_provider_id",
            "capability": "rerank",
            "inherit": None,
        },
    ],
    "meme_manager": [
        {"path": "semantic.vision_provider_id", "capability": "vision", "inherit": None},
        {"path": "semantic.embedding_provider_id", "capability": "embedding", "inherit": None},
        {"path": "auto_collect.vision_provider_id", "capability": "vision", "inherit": None},
        {"path": "generation.emotion.llm.provider_id", "capability": "llm", "inherit": None},
    ],
}

#: 所有携带 API Key 的字段路径（永不进快照）。
_SECRET_PATHS: frozenset[str] = frozenset(field["path"] for field in _SECRET_SETTINGS)


def field_table(kind: str, resource_id: str | None = None) -> list[dict[str, Any]]:
    """返回某类资源的可写字段表（副本，调用方可自由修改）。

    Args:
        kind: 资源类型（``profile`` / ``plugin`` / ``provider``）。
        resource_id: 插件资源的 id；插件字段按插件 id 区分，缺省时返回空表。

    Returns:
        字段定义列表；未知资源类型或未审查的插件返回空列表。
    """
    if kind == "profile":
        return copy.deepcopy([*_CORE_BINDINGS, *_SETTINGS])
    if kind == "plugin":
        if not isinstance(resource_id, str):
            return []
        return copy.deepcopy(_PLUGIN_FIELDS.get(resource_id, []))
    return []


def web_search_provider_options(
    features: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """网页搜索提供商选项。

    原生搜索选项只在读到插件清单且插件已安装时出现；未启用时置 ``disabled``。

    Args:
        features: 快照里的功能开关（``{"nativeWebSearch": {...}}``），可为 ``None``。

    Returns:
        选项列表 ``[{value,label,...}]``；原生选项额外带 ``native``/``disabled``。
    """
    options: list[dict[str, Any]] = [dict(provider) for provider in WEB_SEARCH_PROVIDERS]
    native = (features or {}).get("nativeWebSearch") if isinstance(features, Mapping) else None
    if isinstance(native, Mapping) and native.get("installed"):
        activated = bool(native.get("activated"))
        options.append(
            {
                "value": NATIVE_SEARCH_PLUGIN["value"],
                "label": NATIVE_SEARCH_PLUGIN["label"]
                if activated
                else f"{NATIVE_SEARCH_PLUGIN['label']}（插件未启用）",
                "native": True,
                "disabled": not activated,
            }
        )
    return options


def web_search_key_field(provider: str) -> dict[str, Any] | None:
    """返回内置搜索提供商保存 API Key 的字段定义；未知提供商返回 ``None``。"""
    definition = _WEB_SEARCH_KEY_FIELDS.get(provider) if isinstance(provider, str) else None
    return dict(definition) if definition else None


def is_secret_path(path: str) -> bool:
    """判断某个配置路径是否携带密钥（这类值不得出现在快照/计划里）。"""
    return isinstance(path, str) and path in _SECRET_PATHS


def get_path(obj: Any, path: str) -> Any:
    """按点分路径取值；路径缺失时返回 ``None``（等价旧 ``get``）。"""
    current = obj
    for key in path.split("."):
        if not isinstance(current, Mapping):
            return None
        if key not in current:
            return None
        current = current[key]
    return current


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    """按点分路径写入（自动补建中间对象），等价旧 ``set``。"""
    parts = path.split(".")
    current: dict[str, Any] = obj
    for key in parts[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[parts[-1]] = value


def has_path(obj: Any, path: str) -> bool:
    """判断配置里是否**存在**该路径（存在但为空值也算存在）。"""
    current = obj
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return True


def discover(config: Any, prefix: str = "") -> list[dict[str, Any]]:
    """递归发现未审查的 ``provider_id`` / ``model_id`` 字段（等价旧 ``discover``）。

    Args:
        config: 配置对象。
        prefix: 递归前缀。

    Returns:
        ``[{path, value, reviewRequired: True}]``，按发现顺序。
    """
    found: list[dict[str, Any]] = []
    if not isinstance(config, Mapping):
        return found
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            found.extend(discover(value, path))
        elif isinstance(value, str) and key.endswith(("provider_id", "model_id")):
            found.append({"path": path, "value": value, "reviewRequired": True})
    return found


def build_bindings(
    snapshot_raw: Mapping[str, Any],
    *,
    kind: str = "profile",
    resource_id: str | None = None,
) -> dict[str, Any]:
    """从一份已读取的配置对象生成「未审查字段」清单（旧 ``registry.discover`` 的等价物）。

    Engine 用它填充快照里每个资源的 ``unreviewed``；该资源已审查的字段（含设置项）会被过滤掉。

    Args:
        snapshot_raw: 单个资源的配置对象（如 ``cmd_config.json`` 的内容）。
        kind: 资源类型，决定用哪张字段表过滤。
        resource_id: 插件资源的 id（``kind="plugin"`` 时需要）。

    Returns:
        ``{"discovered": [...], "unreviewed": [{path, reviewRequired}]}``。
    """
    discovered = discover(snapshot_raw)
    reviewed = {field["path"] for field in field_table(kind, resource_id)}
    unreviewed = [
        {"path": item["path"], "reviewRequired": True}
        for item in discovered
        if item["path"] not in reviewed
    ]
    return {"discovered": discovered, "unreviewed": unreviewed}
