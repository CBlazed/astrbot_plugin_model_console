"""HTTP/Agent 适配器、Agent 服务端与 remote.py 编排的单元测试。

覆盖：

* ``HttpGatewayAdapter``：read/write 端点映射、鉴权头、HTTP 错误码、远程快照字段；
* ``AgentAdapter`` / ``pair_agent``：配对、token 鉴权、错误码、写侧 409 透传；
* ``console.agent``：只监听 loopback、一次性配对、状态只存哈希、只读默认、热重载证据；
* ``console.remote``：local 分流、只读强制、plan/apply/backups 白名单与路径脱敏。

全部使用 httpx.MockTransport / 假 adapter / 临时目录，不连真实远程设备、不发真实模型请求。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from console import agent as agent_module
from console import remote
from console.agent import AGENT_READ_ONLY, start_agent
from console.agent_adapter import (
    AGENT_INVALID_RESPONSE,
    AGENT_RATE_LIMITED,
    AGENT_READ_ONLY as ADAPTER_READ_ONLY,
    AGENT_UNAUTHORIZED,
    AGENT_UNREACHABLE,
    AgentAdapter,
    pair_agent,
)
from console.errors import ConsoleError
from console.http_adapter import HttpGatewayAdapter

TOKEN64 = "a" * 64
PLAN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
BACKUP_ID = "0243f684-e8ed-44ac-8c95-657df32890aa"


def ok(data: Any, status: int = 200) -> httpx.Response:
    """OpenAPI 成功响应体。"""
    return httpx.Response(status, json={"status": "ok", "data": data})


def error_response(status: int, payload: dict | None = None) -> httpx.Response:
    """错误响应体。"""
    return httpx.Response(status, json=payload or {})


def make_client(handler) -> httpx.AsyncClient:
    """用 MockTransport 建一个可注入的客户端。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class HttpAdapterReadTests(unittest.IsolatedAsyncioTestCase):
    """``HttpGatewayAdapter`` 读/写端点与错误映射。"""

    async def test_read_maps_endpoints_and_sends_key_in_header_only(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = request.url.path
            if path == "/api/v1/providers/by-id":
                return ok({"provider": {"id": "a", "model": "m"}})
            if path == "/api/v1/config-profiles/default":
                return ok({"config": {"agent_runner": {}}})
            if path == "/api/v1/plugins/config":
                return ok({"config": {"x": 1}})
            return error_response(404)

        client = make_client(handler)
        try:
            adapter = HttpGatewayAdapter(base_url="http://192.168.1.20:6185/",
                                         api_key="abk_test", client=client)
            self.assertEqual((await adapter.read({"kind": "provider", "id": "a"}))["model"], "m")
            self.assertEqual(await adapter.read({"kind": "profile", "id": "default"}),
                             {"agent_runner": {}})
            self.assertEqual(await adapter.read({"kind": "plugin", "id": "p"}), {"x": 1})
        finally:
            await client.aclose()
        for request in calls:
            self.assertEqual(request.headers["Authorization"], "Bearer abk_test")
            self.assertNotIn("abk_test", str(request.url))

    async def test_write_sends_expected_bodies(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return ok({})

        client = make_client(handler)
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k", client=client)
            await adapter.write({"kind": "provider", "id": "a"},
                                {"id": "a", "model": "m"})
            await adapter.write({"kind": "profile", "id": "default"},
                                {"agent_runner": {}})
            await adapter.write({"kind": "plugin", "id": "p"}, {"x": 1})
        finally:
            await client.aclose()
        self.assertEqual(json.loads(captured[0].content),
                         {"provider_id": "a", "config": {"id": "a", "model": "m"}})
        self.assertEqual(json.loads(captured[1].content), {"agent_runner": {}})
        self.assertEqual(json.loads(captured[2].content),
                         {"plugin_id": "p", "config": {"x": 1}})

    async def test_http_failures_map_to_stable_codes(self) -> None:
        cases = [
            (lambda _r: error_response(401), "DEVICE_AUTH_FAILED"),
            (lambda _r: error_response(403), "DEVICE_FORBIDDEN"),
            (lambda _r: error_response(500), "DEVICE_HTTP_500"),
            (lambda _r: httpx.Response(200, content=b"not json"), "DEVICE_INVALID_RESPONSE"),
            (lambda _r: httpx.Response(200, json={"status": "error"}),
             "DEVICE_API_REJECTED"),
        ]
        for handler, code in cases:
            with self.subTest(code=code):
                client = make_client(handler)
                try:
                    adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                                 client=client)
                    with self.assertRaises(ConsoleError) as ctx:
                        await adapter.read({"kind": "profile", "id": "default"})
                    self.assertEqual(ctx.exception.code, code)
                finally:
                    await client.aclose()

        async def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("ECONNREFUSED")

        client = make_client(boom)
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                         client=client)
            with self.assertRaises(ConsoleError) as ctx:
                await adapter.read({"kind": "profile", "id": "default"})
            self.assertEqual(ctx.exception.code, "DEVICE_UNREACHABLE")
        finally:
            await client.aclose()

    async def test_invalid_constructor_and_path_arguments(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            HttpGatewayAdapter(base_url="", api_key="k")
        self.assertEqual(ctx.exception.code, "DEVICE_INVALID")
        with self.assertRaises(ConsoleError) as ctx:
            HttpGatewayAdapter(base_url="http://h:1", api_key="")
        self.assertEqual(ctx.exception.code, "DEVICE_SECRET_REQUIRED")

        async def handler(_request: httpx.Request) -> httpx.Response:
            return ok({})

        client = make_client(handler)
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k", client=client)
            with self.assertRaises(ConsoleError) as ctx:
                await adapter.call("GET", "/elsewhere")
            self.assertEqual(ctx.exception.code, "DEVICE_INVALID")
            with self.assertRaises(ConsoleError) as ctx:
                await adapter.read({"kind": "unknown", "id": "x"})
            self.assertEqual(ctx.exception.code, "DEVICE_INVALID")
        finally:
            await client.aclose()

    async def test_efforts_is_none_like_node_adapter(self) -> None:
        client = make_client(lambda _r: ok({}))
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                         client=client)
            self.assertIsNone(await adapter.efforts("a"))
        finally:
            await client.aclose()

    async def test_resources_render_default_first_with_remote_identity(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/config-profiles":
                return ok({"info_list": [{"id": "default", "name": "Default"},
                                         {"id": "p1", "name": "远程档案"}]})
            if path == "/api/v1/plugins":
                return ok({"plugins": [{"name": "astrbot_plugin_a"}, {"id": "b"}]})
            if path == "/api/v1/config-routes":
                return ok({"routing": {"snow::": "p1", "qq::": "p2", "a::b": "p3"}})
            return error_response(404)

        client = make_client(handler)
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                         client=client)
            resources = await adapter.resources()
            self.assertEqual([f"{item['kind']}:{item['id']}" for item in resources],
                             ["profile:default", "profile:p1",
                              "plugin:astrbot_plugin_a", "plugin:b"])
            self.assertIn("远程", resources[0]["identity"]["name"])
            self.assertEqual(resources[0]["identity"]["association"], "remote-default")
            self.assertEqual(await adapter.routing_summary(),
                             {"total": 3, "platformLevel": 2, "granular": 1})
            self.assertIn("会话级", (await adapter.resources())[1]["identity"]["routingStatus"])
        finally:
            await client.aclose()

    async def test_routing_summary_and_health_degrade_on_errors(self) -> None:
        client = make_client(lambda _r: error_response(401))
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                         client=client)
            self.assertEqual(await adapter.routing_summary(),
                             {"total": 0, "platformLevel": 0, "granular": 0})
            health = await adapter.health()
            self.assertFalse(health["reachable"])
            self.assertEqual(health["error"], "DEVICE_AUTH_FAILED")
        finally:
            await client.aclose()


class HttpAdapterSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """远程快照字段与旧 ``Engine.loadSnapshot`` 口径。"""

    def api_handler(self, requests: list[httpx.Request]):
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            path = request.url.path
            if path == "/api/v1/config-profiles":
                return ok({"info_list": [{"id": "default", "name": "Default"},
                                         {"id": "p1", "name": "远程"}]})
            if path == "/api/v1/config-profiles/default":
                return ok({"config": {
                    "config_version": 3,
                    "agent_runner": {"config": {"model": {"provider_id": "a"}}},
                    "provider": [{"id": "a", "model": "m", "provider_source_id": "s",
                                  "enable": True,
                                  "custom_extra_body": {"reasoning_effort": "high"}}],
                    "provider_sources": [{"id": "s", "type": "openai_chat_completion"}],
                    "provider_settings": {"web_search": True},
                    "legacy_provider_id": "z",
                }})
            if path == "/api/v1/config-profiles/p1":
                return ok({"config": {}})
            if path == "/api/v1/config-routes":
                return ok({"routing": {"qq::": "p1"}})
            if path == "/api/v1/plugins":
                return ok({"plugins": [{"name": "astrbot_plugin_a", "activated": True}]})
            if path == "/api/v1/plugins/config":
                return ok({"config": {}})
            return error_response(404)

        return handler

    async def test_snapshot_fields_models_bindings_and_unreviewed(self) -> None:
        requests: list[httpx.Request] = []
        client = make_client(self.api_handler(requests))
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                         client=client)
            snapshot = await adapter.snapshot()
        finally:
            await client.aclose()
        self.assertEqual(snapshot["writeEnabled"], False)
        self.assertEqual(snapshot["configVersion"], 3)
        self.assertEqual(snapshot["routing"], {"total": 1, "platformLevel": 1, "granular": 0})
        self.assertEqual(snapshot["models"][0]["id"], "a")
        self.assertEqual(snapshot["models"][0]["capabilities"], ["llm"])
        self.assertEqual(snapshot["models"][0]["effort"], "high")
        self.assertEqual(snapshot["models"][0]["resource"],
                         {"kind": "provider", "id": "a"})
        # 资源顺序：default profile + p1 + plugin
        self.assertEqual([item["resource"]["kind"] for item in snapshot["resources"]],
                         ["profile", "profile", "plugin"])
        default_resource = snapshot["resources"][0]
        self.assertIn("revision", default_resource)
        paths = [row["path"] for row in default_resource["bindings"]]
        self.assertIn("provider_settings.web_search", paths)
        web_search = next(row for row in default_resource["bindings"]
                          if row["path"] == "provider_settings.web_search")
        self.assertEqual(web_search["value"], "true")
        self.assertEqual(web_search["status"], "configured")
        model_row = next(row for row in default_resource["bindings"]
                         if row["path"] == "agent_runner.config.model.provider_id")
        self.assertEqual(model_row["effectiveModel"], "a")
        self.assertEqual(model_row["status"], "configured")
        self.assertEqual(default_resource["unreviewed"],
                         [{"path": "legacy_provider_id", "reviewRequired": True}])
        # 远程 identity 口径
        self.assertEqual(snapshot["resources"][0]["resource"]["identity"]["transport"],
                         "远程设备")
        self.assertEqual(snapshot["features"]["nativeWebSearch"]["installed"], False)
        self.assertIsInstance(snapshot["readAt"], str)

    async def test_unreadable_resource_is_marked_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/config-profiles":
                return ok({"info_list": [{"id": "p1", "name": "远程"}]})
            if path == "/api/v1/config-profiles/default":
                return ok({"config": {}})
            if path == "/api/v1/config-profiles/p1":
                return error_response(500)
            if path == "/api/v1/config-routes":
                return ok({"routing": {}})
            if path == "/api/v1/plugins":
                return error_response(404)
            return error_response(404)

        client = make_client(handler)
        try:
            adapter = HttpGatewayAdapter(base_url="http://h:1", api_key="k",
                                         client=client)
            snapshot = await adapter.snapshot()
        finally:
            await client.aclose()
        self.assertEqual(snapshot["resources"][1],
                         {"resource": snapshot["resources"][1]["resource"],
                          "status": "unavailable"})
        self.assertIsNone(snapshot["plugins"])
        self.assertFalse(snapshot["features"]["nativeWebSearch"]["listAvailable"])


class AgentAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Hub 侧 agent 客户端。"""

    async def test_pair_posts_code_and_returns_token(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"token": TOKEN64})

        client = make_client(handler)
        try:
            result = await pair_agent({"baseUrl": "http://127.0.0.1:6000"},
                                      "12345678", client=client)
        finally:
            await client.aclose()
        self.assertEqual(result, TOKEN64)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.path, "/agent/v1/pair")
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(json.loads(calls[0].content)["code"], "12345678")

    async def test_pair_uses_injected_client_via_adapter(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"token": TOKEN64})

        client = make_client(handler)
        try:
            adapter = AgentAdapter(base_url="http://127.0.0.1:6000/", client=client)
            self.assertEqual(await adapter.pair("12345678"), TOKEN64)
            self.assertEqual(adapter.token, TOKEN64)
            self.assertEqual(calls[0].url.path, "/agent/v1/pair")
        finally:
            await client.aclose()

    async def test_health_is_public_and_snapshot_requires_token(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path.endswith("/health"):
                return httpx.Response(200, json={"status": "ok", "version": "0.1.0"})
            if request.url.path.endswith("/pair"):
                return httpx.Response(200, json={"token": TOKEN64})
            return httpx.Response(200, json={"readAt": "x", "writeEnabled": False})

        client = make_client(handler)
        try:
            adapter = AgentAdapter(base_url="http://127.0.0.1:6000", client=client)
            self.assertEqual(await adapter.health(),
                             {"status": "ok", "version": "0.1.0"})
            self.assertNotIn("Authorization", calls[0].headers)
            with self.assertRaises(ConsoleError) as ctx:
                await adapter.snapshot()
            self.assertEqual(ctx.exception.code, AGENT_UNAUTHORIZED)
            self.assertEqual(len(calls), 1, "snapshot without token must fail locally")
            self.assertEqual(await adapter.pair("12345678"), TOKEN64)
        finally:
            await client.aclose()

    async def test_http_failures_map_to_agent_error_codes(self) -> None:
        async def make(fetcher, **kwargs) -> AgentAdapter:
            return AgentAdapter(base_url="http://127.0.0.1:6000", token=TOKEN64,
                                client=make_client(fetcher), **kwargs)

        cases = [
            (lambda _r: (_ for _ in ()).throw(httpx.ConnectError("ECONNREFUSED")),
             AGENT_UNREACHABLE),
            (lambda _r: httpx.Response(401, json={"error": "AGENT_UNAUTHORIZED"}),
             AGENT_UNAUTHORIZED),
            (lambda _r: httpx.Response(429, json={"error": "AGENT_RATE_LIMITED"}),
             AGENT_RATE_LIMITED),
            (lambda _r: httpx.Response(500, json={"error": "AGENT_SNAPSHOT_FAILED"}),
             AGENT_INVALID_RESPONSE),
            (lambda _r: httpx.Response(200, content=b"bad json"), AGENT_INVALID_RESPONSE),
        ]
        for handler, code in cases:
            with self.subTest(code=code):
                adapter = await make(handler)
                try:
                    with self.assertRaises(ConsoleError) as ctx:
                        await adapter.snapshot()
                    self.assertEqual(ctx.exception.code, code)
                finally:
                    await adapter.close()
        for handler, code, method in (
            (lambda _r: httpx.Response(200, json={"status": "degraded"}),
             AGENT_INVALID_RESPONSE, "health"),
            (lambda _r: httpx.Response(200, json={"token": "short"}),
             AGENT_INVALID_RESPONSE, "pair"),
        ):
            adapter = await make(handler)
            try:
                with self.assertRaises(ConsoleError) as ctx:
                    if method == "health":
                        await adapter.health()
                    else:
                        await adapter.pair("12345678")
                self.assertEqual(ctx.exception.code, code)
            finally:
                await adapter.close()

    def test_constructor_rejects_bad_url_and_sanitizes_token(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            AgentAdapter(base_url="")
        self.assertEqual(ctx.exception.code, "AGENT_INVALID_BASE_URL")
        with self.assertRaises(ConsoleError) as ctx:
            AgentAdapter(base_url="ftp://127.0.0.1:1")
        self.assertEqual(ctx.exception.code, "AGENT_INVALID_BASE_URL")
        adapter = AgentAdapter(base_url="http://127.0.0.1:6000/",
                               token="not-a-token")
        self.assertEqual(adapter.base_url, "http://127.0.0.1:6000")
        self.assertIsNone(adapter.token)

    async def test_plan_apply_backups_escape_hatches(self) -> None:
        plan_body = {"id": PLAN_ID, "resource": {"kind": "profile", "id": "default"},
                     "changes": [{"path": "x", "value": "y"}],
                     "expiresAt": "2026-10-29T22:10:00.000Z"}
        apply_body = {"result": {"status": "saved-and-readback-verified", "id": PLAN_ID},
                      "hotLoad": {"verified": True, "checked": True}}
        backups_body = {"backups": [], "stats": {"count": 0, "bytes": 0},
                        "limits": {"keep": 10, "maxBytes": 1}}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/plans"):
                return httpx.Response(200, json=plan_body)
            if path.endswith("/apply"):
                return httpx.Response(200, json=apply_body)
            if path.endswith("/backups"):
                return httpx.Response(200, json=backups_body)
            return httpx.Response(404)

        client = make_client(handler)
        try:
            adapter = AgentAdapter(base_url="http://127.0.0.1:6000", token=TOKEN64,
                                   client=client)
            self.assertEqual(await adapter.plan({"resource": {"kind": "profile",
                                                              "id": "default"},
                                                 "changes": [{"path": "x",
                                                              "value": "y"}],
                                                 "expectedRevision": "rev-1"}),
                             plan_body)
            self.assertEqual(await adapter.apply({"id": PLAN_ID}), apply_body)
            self.assertEqual(await adapter.backups(), backups_body)
        finally:
            await client.aclose()

    async def test_read_only_and_relayed_409_errors(self) -> None:
        def readonly(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "AGENT_READ_ONLY"})

        client = make_client(readonly)
        try:
            adapter = AgentAdapter(base_url="http://127.0.0.1:6000", token=TOKEN64,
                                   client=client)
            for coro in (adapter.plan({"resource": {}, "changes": []}),
                         adapter.apply({"id": PLAN_ID}),
                         adapter.backups()):
                with self.assertRaises(ConsoleError) as ctx:
                    await coro
                self.assertEqual(ctx.exception.code, ADAPTER_READ_ONLY)
        finally:
            await client.aclose()

        def conflict(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={
                "error": "CONCURRENT_CHANGE", "id": PLAN_ID,
                "details": {"id": PLAN_ID, "outcome": "not-written"},
                "token": "abc",
            })

        client = make_client(conflict)
        try:
            adapter = AgentAdapter(base_url="http://127.0.0.1:6000", token=TOKEN64,
                                   client=client)
            with self.assertRaises(ConsoleError) as ctx:
                await adapter.apply({"id": PLAN_ID})
            self.assertEqual(ctx.exception.code, "CONCURRENT_CHANGE")
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.details,
                             {"id": PLAN_ID, "outcome": "not-written"})
        finally:
            await client.aclose()


class FakeEngine:
    """Agent 服务端测试用的内存引擎。"""

    def __init__(self, *, write_enabled: bool = False) -> None:
        self.write_enabled = write_enabled
        self.applied: list[str] = []

    async def snapshot(self) -> dict:
        return {"readAt": "2026-10-29T00:00:00.000Z",
                "writeEnabled": self.write_enabled, "models": [], "resources": []}

    async def plan(self, resource: Any, changes: Any, restore: bool = False,
                   expected_revision: Any = None) -> dict:
        return {"id": PLAN_ID, "resource": resource, "changes": changes,
                "revision": "rev-1", "warning": "warn",
                "expiresAt": "2026-10-29T22:10:00.000Z"}

    async def apply(self, plan_id: str) -> dict:
        self.applied.append(plan_id)
        return {"status": "saved-and-readback-verified", "id": plan_id}

    async def list_backups(self) -> list[dict]:
        return [{"id": BACKUP_ID, "createdAt": "2026-10-29T10:00:00.000Z",
                 "bytes": 100, "files": 8, "sqlite": "ok", "internal": "drop-me"}]

    async def backup_stats(self) -> dict:
        return {"count": 1, "bytes": 100, "items": [{"internal": True}]}


class AgentServerTests(unittest.IsolatedAsyncioTestCase):
    """``console.agent`` 真实 loopback 服务。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="model-console-agent-"))
        self.state = self.dir / "state"
        self.data = self.dir / "data"
        (self.data / "logs").mkdir(parents=True)
        (self.data / "logs" / "astrbot.log").write_text(
            "boot\n配置已更新，已热重载\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _start(self, engine: FakeEngine, *, write_enabled: bool = False,
                     **kwargs) -> Any:
        return await start_agent(engine=engine, data_dir=self.data,
                                 state_dir=self.state, write_enabled=write_enabled,
                                 **kwargs)

    async def _pair(self, client: httpx.AsyncClient, handle: Any,
                    code: str) -> httpx.Response:
        return await client.post(f"{handle.url}/agent/v1/pair",
                                 json={"code": code})

    async def test_health_is_public_and_loopback_only(self) -> None:
        handle = await self._start(FakeEngine())
        try:
            self.assertEqual(handle.url, f"http://127.0.0.1:{handle.port}")
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{handle.url}/agent/v1/health")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(sorted(body), ["status", "version"])
                self.assertEqual(body["status"], "ok")
                self.assertEqual((await client.get(
                    f"{handle.url}/agent/v1/nope")).status_code, 404)
                self.assertEqual((await client.get(
                    f"{handle.url}/agent/v1/snapshot")).status_code, 401)
        finally:
            await handle.close()

    async def test_pairing_issues_one_token_and_burns_the_code(self) -> None:
        handle = await self._start(FakeEngine())
        try:
            self.assertRegex(handle.pairing_code, r"^\d{8}$")
            state_text = handle.state_path.read_text("utf-8")
            state = json.loads(state_text)
            self.assertIsNone(state["tokenHash"])
            self.assertEqual(state["pairing"]["hash"],
                             hashlib.sha256(handle.pairing_code.encode()).hexdigest())
            self.assertNotIn(handle.pairing_code, state_text)
            async with httpx.AsyncClient() as client:
                response = await self._pair(client, handle, handle.pairing_code)
                self.assertEqual(response.status_code, 200)
                token = response.json()["token"]
                self.assertRegex(token, r"^[a-f0-9]{64}$")
                after_text = handle.state_path.read_text("utf-8")
                after = json.loads(after_text)
                self.assertEqual(after["tokenHash"],
                                 hashlib.sha256(token.encode()).hexdigest())
                self.assertIsNone(after["pairing"])
                self.assertNotIn(token, after_text)
                self.assertNotIn(handle.pairing_code, after_text)
                authorized = await client.get(
                    f"{handle.url}/agent/v1/snapshot",
                    headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(authorized.status_code, 200)
                replay = await self._pair(client, handle, handle.pairing_code)
                self.assertEqual(replay.status_code, 401)
                self.assertEqual(replay.json(), {"error": "AGENT_UNAUTHORIZED"})
                wrong = await client.get(
                    f"{handle.url}/agent/v1/snapshot",
                    headers={"Authorization": f"Bearer {'0' * 64}"})
                self.assertEqual(wrong.status_code, 401)
        finally:
            await handle.close()

    async def test_pair_rejects_malformed_requests_and_wrong_code(self) -> None:
        handle = await self._start(FakeEngine())
        try:
            wrong = f"{(int(handle.pairing_code) + 1) % 100000000:08d}"
            async with httpx.AsyncClient() as client:
                rejected = await self._pair(client, handle, wrong)
                self.assertEqual(rejected.status_code, 401)
                self.assertEqual(rejected.json(), {"error": "AGENT_UNAUTHORIZED"})
                bad_json = await client.post(
                    f"{handle.url}/agent/v1/pair",
                    headers={"Content-Type": "application/json"}, content=b"not json")
                self.assertEqual(bad_json.status_code, 400)
                no_type = await client.post(f"{handle.url}/agent/v1/pair",
                                            content=b"{}")
                self.assertEqual(no_type.status_code, 415)
                missing = await client.post(
                    f"{handle.url}/agent/v1/pair",
                    headers={"Content-Type": "application/json"}, content=b"{}")
                self.assertEqual(missing.status_code, 400)
                ok_pair = await self._pair(client, handle, handle.pairing_code)
                self.assertEqual(ok_pair.status_code, 200)
        finally:
            await handle.close()

    async def test_failed_pair_attempts_are_rate_limited(self) -> None:
        handle = await self._start(FakeEngine(), pair_fail_limit=2)
        try:
            wrong = f"{(int(handle.pairing_code) + 1) % 100000000:08d}"
            async with httpx.AsyncClient() as client:
                for _ in range(2):
                    self.assertEqual(
                        (await self._pair(client, handle, wrong)).status_code, 401)
                limited = await self._pair(client, handle, handle.pairing_code)
                self.assertEqual(limited.status_code, 429)
                self.assertEqual(limited.json(), {"error": "AGENT_RATE_LIMITED"})
        finally:
            await handle.close()

    async def test_expired_pairing_code_is_rejected(self) -> None:
        handle = await self._start(FakeEngine(), pairing_ttl_ms=1)
        try:
            await asyncio.sleep(0.02)
            async with httpx.AsyncClient() as client:
                self.assertEqual(
                    (await self._pair(client, handle, handle.pairing_code)).status_code,
                    401)
                self.assertEqual((await client.get(
                    f"{handle.url}/agent/v1/health")).status_code, 200)
        finally:
            await handle.close()

    async def test_read_only_agent_rejects_writes(self) -> None:
        engine = FakeEngine()
        handle = await self._start(engine)
        try:
            async with httpx.AsyncClient() as client:
                token = (await self._pair(client, handle,
                                          handle.pairing_code)).json()["token"]
                headers = {"Authorization": f"Bearer {token}"}
                body = {"resource": {"kind": "profile", "id": "default"},
                        "changes": []}
                for path, payload in (("/agent/v1/plans", body),
                                      ("/agent/v1/apply", {"id": PLAN_ID})):
                    response = await client.post(f"{handle.url}{path}",
                                                 headers=headers, json=payload)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json(), {"error": AGENT_READ_ONLY})
                backups = await client.get(f"{handle.url}/agent/v1/backups",
                                           headers=headers)
                self.assertEqual(backups.status_code, 403)
                self.assertEqual(engine.applied, [])
        finally:
            await handle.close()

    async def test_write_enabled_agent_proxies_plan_apply_backups(self) -> None:
        engine = FakeEngine(write_enabled=True)
        handle = await self._start(engine, write_enabled=True)
        try:
            async with httpx.AsyncClient() as client:
                token = (await self._pair(client, handle,
                                          handle.pairing_code)).json()["token"]
                headers = {"Authorization": f"Bearer {token}"}
                plan = await client.post(
                    f"{handle.url}/agent/v1/plans", headers=headers,
                    json={"resource": {"kind": "profile", "id": "default"},
                          "changes": [{"path": "x", "value": "y"}],
                          "expectedRevision": "rev-1"})
                self.assertEqual(plan.status_code, 200)
                self.assertEqual(plan.json()["id"], PLAN_ID)
                applied = await client.post(
                    f"{handle.url}/agent/v1/apply", headers=headers,
                    json={"id": PLAN_ID})
                self.assertEqual(applied.status_code, 200)
                applied_body = applied.json()
                self.assertEqual(applied_body["result"]["id"], PLAN_ID)
                self.assertTrue(applied_body["hotLoad"]["verified"])
                self.assertTrue(applied_body["hotLoad"]["checked"])
                backups = await client.get(f"{handle.url}/agent/v1/backups",
                                           headers=headers)
                self.assertEqual(backups.status_code, 200)
                backup_body = backups.json()
                self.assertEqual(backup_body["backups"][0]["id"], BACKUP_ID)
                self.assertNotIn("internal", json.dumps(backup_body))
                self.assertEqual(backup_body["stats"], {"count": 1, "bytes": 100})
                self.assertEqual(backup_body["limits"]["keep"], 10)
                self.assertEqual(engine.applied, [PLAN_ID])
        finally:
            await handle.close()

    async def test_engine_errors_are_relayed_with_sanitized_details(self) -> None:
        class FailingEngine(FakeEngine):
            async def apply(self, _plan_id: str) -> dict:
                error = ConsoleError(
                    "CONCURRENT_CHANGE", details={
                        "id": PLAN_ID, "backupId": BACKUP_ID,
                        "outcome": "not-written",
                        "path": "C:\\Users\\alice\\secret",
                        "code": "CONCURRENT_CHANGE",
                    })
                raise error

        handle = await self._start(FailingEngine(), write_enabled=True)
        try:
            async with httpx.AsyncClient() as client:
                token = (await self._pair(client, handle,
                                          handle.pairing_code)).json()["token"]
                response = await client.post(
                    f"{handle.url}/agent/v1/apply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"id": PLAN_ID})
                self.assertEqual(response.status_code, 409)
                body = response.json()
                self.assertEqual(body["error"], "CONCURRENT_CHANGE")
                self.assertEqual(body["outcome"], "not-written")
                self.assertNotIn("path", body)
                self.assertNotIn("alice", json.dumps(body))
        finally:
            await handle.close()

    async def test_snapshot_failure_returns_stable_code(self) -> None:
        class BrokenEngine(FakeEngine):
            async def snapshot(self) -> dict:
                raise RuntimeError("boom")

        handle = await self._start(BrokenEngine())
        try:
            async with httpx.AsyncClient() as client:
                token = (await self._pair(client, handle,
                                          handle.pairing_code)).json()["token"]
                response = await client.get(
                    f"{handle.url}/agent/v1/snapshot",
                    headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"error": "AGENT_SNAPSHOT_FAILED"})
        finally:
            await handle.close()


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    """``console.remote`` 的 7 个入口与白名单。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="model-console-remote-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_registry(self, *devices: dict) -> None:
        (self.dir / "devices.json").write_text(
            json.dumps({"version": 1, "devices": list(devices)}),
            encoding="utf-8")
        (self.dir / "secrets.json").write_text(
            json.dumps({"version": 2,
                        "secrets": {device["id"]: "plain-secret" for device in devices}}),
            encoding="utf-8")

    def _device(self, **overrides: Any) -> dict:
        device = {"id": "dev-1", "name": "远程", "kind": "http",
                  "baseUrl": "http://127.0.0.1:6185", "readOnly": True,
                  "auth": {"type": "api-key"}}
        device.update(overrides)
        return device

    async def test_local_device_and_find_device(self) -> None:
        self.assertEqual(remote.local_device(read_only=False), {
            "id": "local", "name": "本机", "kind": "local",
            "readOnly": False, "hasSecret": False})
        self.assertEqual((await remote.find_device("local", read_only=True))["kind"],
                         "local")
        self.assertIsNone(await remote.find_device("missing", registry_dir=self.dir))
        device = self._device()
        self._write_registry(device)
        found = await remote.find_device("dev-1", registry_dir=self.dir)
        self.assertEqual(found["id"], "dev-1")
        self.assertTrue(found["hasSecret"])

    async def test_health_local_and_remote_public_shape(self) -> None:
        self.assertEqual(await remote.remote_health(
            {"id": "local", "kind": "local"}, registry_dir=self.dir),
            {"reachable": True})

        class Adapter:
            async def health(self) -> dict:
                return {"status": "ok", "version": "0.1.0"}

            async def close(self) -> None:
                pass

        device = self._device(kind="agent", baseUrl="http://127.0.0.1:9000")
        self._write_registry(device)
        with mock.patch.object(remote, "_make_agent_adapter", return_value=Adapter()):
            health = await remote.remote_health(device, registry_dir=self.dir)
        self.assertTrue(health["reachable"])
        self.assertIsInstance(health["latencyMs"], int)

        class DeadAdapter:
            async def health(self) -> dict:
                return {"reachable": False, "latencyMs": 3,
                        "error": "DEVICE_AUTH_FAILED", "extra": "drop-me"}

            async def close(self) -> None:
                pass

        with mock.patch.object(remote, "_make_agent_adapter", return_value=DeadAdapter()):
            health = await remote.remote_health(device, registry_dir=self.dir)
        self.assertEqual(health, {"reachable": False, "latencyMs": 3,
                                  "error": "DEVICE_AUTH_FAILED"})

    async def test_snapshot_forces_read_only_and_tags_device(self) -> None:
        class Adapter:
            def __init__(self, payload: dict) -> None:
                self.payload = payload
                self.closed = False

            async def snapshot(self) -> dict:
                return self.payload

            async def close(self) -> None:
                self.closed = True

        base = {"models": [{"id": "m"}], "resources": []}
        agent_writable = self._device(id="agent-w", name="Agent", kind="agent",
                                      readOnly=False)
        http_device = self._device(id="http-1")
        agent_read_only = self._device(id="agent-ro", kind="agent", readOnly=True)
        self._write_registry(agent_writable, http_device, agent_read_only)
        adapter = Adapter({**base, "writeEnabled": True})
        with mock.patch.object(remote, "_make_agent_adapter", return_value=adapter):
            result = await remote.remote_snapshot(agent_writable, registry_dir=self.dir)
        self.assertTrue(result["writeEnabled"])
        self.assertEqual(result["device"],
                         {"id": "agent-w", "name": "Agent", "kind": "agent"})
        self.assertTrue(adapter.closed)

        adapter = Adapter({**base, "writeEnabled": True})
        with mock.patch.object(remote, "_make_http_adapter", return_value=adapter):
            result = await remote.remote_snapshot(http_device, registry_dir=self.dir)
        self.assertFalse(result["writeEnabled"], "http 快照必须强制只读")

        adapter = Adapter({**base, "writeEnabled": True})
        with mock.patch.object(remote, "_make_agent_adapter", return_value=adapter):
            result = await remote.remote_snapshot(agent_read_only,
                                                  registry_dir=self.dir)
        self.assertFalse(result["writeEnabled"])

    async def test_snapshot_rejects_local_and_bad_payload(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            await remote.remote_snapshot({"id": "local", "kind": "local"},
                                         registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, "DEVICE_INVALID")

        class BadAdapter:
            async def snapshot(self) -> list:
                return []

            async def close(self) -> None:
                pass

        device = self._device()
        self._write_registry(device)
        with mock.patch.object(remote, "_make_http_adapter", return_value=BadAdapter()):
            with self.assertRaises(ConsoleError) as ctx:
                await remote.remote_snapshot(device, registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, "DEVICE_INVALID_RESPONSE")

    async def test_ssh_snapshot_opens_and_closes_tunnel(self) -> None:
        opened: list[dict] = []
        closed: list[bool] = []

        class Tunnel:
            async def start(self) -> str:
                return "http://127.0.0.1:4567"

            async def close(self) -> None:
                closed.append(True)

        def open_tunnel(*, ssh: dict, base_url: str, data_path: Any) -> Tunnel:
            opened.append({"ssh": ssh, "baseUrl": base_url, "dataPath": data_path})
            return Tunnel()

        captured: list[dict] = []

        class Adapter:
            async def snapshot(self) -> dict:
                return {"models": [], "resources": []}

            async def close(self) -> None:
                pass

        def make_adapter(*, base_url: str, api_key: str) -> Adapter:
            captured.append({"baseUrl": base_url, "apiKey": api_key})
            return Adapter()

        device = self._device(id="ssh-1", kind="ssh",
                              ssh={"host": "10.0.0.2", "user": "root",
                                   "dataPath": "C:\\AstrBot\\data"})
        self._write_registry(device)
        with mock.patch.object(remote, "_open_ssh_tunnel", side_effect=open_tunnel), \
                mock.patch.object(remote, "_make_http_adapter", side_effect=make_adapter):
            await remote.remote_snapshot(device, registry_dir=self.dir)
        self.assertEqual(opened[0]["dataPath"], "C:\\AstrBot\\data")
        self.assertEqual(captured[0]["baseUrl"], "http://127.0.0.1:4567")
        self.assertEqual(captured[0]["apiKey"], "plain-secret")
        self.assertEqual(closed, [True])

    async def test_snapshot_failures_before_any_session(self) -> None:
        device = self._device()
        self._write_registry()
        with self.assertRaises(ConsoleError) as ctx:
            await remote.remote_snapshot(device, registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, "DEVICE_SECRET_REQUIRED")

    async def test_agent_plan_whitelists_fields(self) -> None:
        device = self._device(id="agent-w", kind="agent", readOnly=False)
        self._write_registry(device)

        class Adapter:
            async def plan(self, body: dict) -> dict:
                return {"id": PLAN_ID,
                        "resource": {"kind": "profile", "id": "default", "x": 1},
                        "revision": "rev-1",
                        "changes": [{"path": "p", "before": None, "value": "v",
                                     "capability": "llm", "secret": True}],
                        "expiresAt": "2026-10-29T22:10:00.000Z",
                        "impact": "this profile", "warning": "warn",
                        "internal": "drop-me"}

            async def close(self) -> None:
                pass

        with mock.patch.object(remote, "_make_agent_adapter", return_value=Adapter()):
            result = await remote.remote_agent_plan(
                device, {"resource": {"kind": "profile", "id": "default"},
                         "changes": [{"path": "p", "value": "v"}]},
                registry_dir=self.dir)
        self.assertEqual(result["id"], PLAN_ID)
        self.assertEqual(result["resource"], {"kind": "profile", "id": "default"})
        self.assertEqual(result["changes"], [{
            "path": "p", "before": None, "value": "v", "capability": "llm"}])
        self.assertNotIn("internal", result)

    async def test_apply_keeps_uuid_backup_and_redacts_paths(self) -> None:
        device = self._device(id="agent-w", kind="agent", readOnly=False)
        self._write_registry(device)
        backup_path = "C:\\Users\\alice\\backups\\" + BACKUP_ID

        class Adapter:
            async def apply(self, body: dict) -> dict:
                return {
                    "result": {"status": "saved-and-readback-verified",
                               "id": body["id"], "backupId": backup_path},
                    "hotLoad": {"verified": True, "checked": True,
                                "marker": "reload at C:\\secret\\path",
                                "note": "hot reload detected"},
                }

            async def close(self) -> None:
                pass

        with mock.patch.object(remote, "_make_agent_adapter", return_value=Adapter()):
            result = await remote.remote_agent_apply(
                device, {"id": PLAN_ID}, registry_dir=self.dir)
        self.assertEqual(result["status"], "saved-and-readback-verified")
        self.assertEqual(result["backupId"], BACKUP_ID)
        self.assertEqual(result["hotLoad"], {"verified": True, "checked": True,
                                             "marker": "reload at [path]",
                                             "note": "hot reload detected"})
        self.assertNotIn("alice", json.dumps(result))

    async def test_backups_normalize_and_whitelist(self) -> None:
        device = self._device(id="agent-w", kind="agent", readOnly=True)
        self._write_registry(device)

        class Adapter:
            async def backups(self) -> dict:
                return {
                    "backups": [{"id": BACKUP_ID,
                                 "createdAt": "2026-10-29T10:00:00.000Z",
                                 "bytes": 100.5, "files": 8, "sqlite": "ok",
                                 "internal": "drop-me"}],
                    "stats": {"count": 1, "bytes": 100, "items": [{"internal": True}]},
                    "limits": {"keep": 5, "maxBytes": 1024},
                }

            async def close(self) -> None:
                pass

        with mock.patch.object(remote, "_make_agent_adapter", return_value=Adapter()):
            result = await remote.remote_agent_backups(device, registry_dir=self.dir)
        self.assertEqual(result, {
            "backups": [{"id": BACKUP_ID,
                         "createdAt": "2026-10-29T10:00:00.000Z",
                         "bytes": 100, "files": 8, "sqlite": "ok"}],
            "stats": {"count": 1, "bytes": 100},
            "limits": {"keep": 5, "maxBytes": 1024},
        })
        self.assertNotIn("internal", json.dumps(result))

    async def test_write_permissions_and_error_codes(self) -> None:
        http_device = self._device(id="http-1", kind="http")
        for call in (
            remote.remote_agent_plan(http_device, {}, registry_dir=self.dir),
            remote.remote_agent_apply(http_device, {}, registry_dir=self.dir),
        ):
            with self.assertRaises(ConsoleError) as ctx:
                await call
            self.assertEqual(ctx.exception.code, "DEVICE_READ_ONLY")

        readonly_agent = self._device(id="agent-ro", kind="agent", readOnly=True)
        with self.assertRaises(ConsoleError) as ctx:
            await remote.remote_agent_plan(readonly_agent, {}, registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, "DEVICE_READ_ONLY")

        with self.assertRaises(ConsoleError) as ctx:
            await remote.remote_agent_backups(http_device, registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, "DEVICE_READ_ONLY")

        class FailingAdapter:
            async def plan(self, _body: dict) -> dict:
                raise ConsoleError(ADAPTER_READ_ONLY, details={
                    "path": "C:\\secret", "id": PLAN_ID})

            async def close(self) -> None:
                pass

        writable = self._device(id="agent-w", kind="agent", readOnly=False)
        self._write_registry(http_device, readonly_agent, writable)
        with mock.patch.object(remote, "_make_agent_adapter",
                               return_value=FailingAdapter()):
            with self.assertRaises(ConsoleError) as ctx:
                await remote.remote_agent_plan(writable, {}, registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, ADAPTER_READ_ONLY)
        self.assertEqual(ctx.exception.details, {"id": PLAN_ID})

    async def test_unknown_agent_response_is_invalid(self) -> None:
        device = self._device(id="agent-w", kind="agent", readOnly=False)
        self._write_registry(device)

        class Adapter:
            async def plan(self, _body: dict) -> list:
                return []

            async def close(self) -> None:
                pass

        with mock.patch.object(remote, "_make_agent_adapter", return_value=Adapter()):
            with self.assertRaises(ConsoleError) as ctx:
                await remote.remote_agent_plan(device, {}, registry_dir=self.dir)
        self.assertEqual(ctx.exception.code, "DEVICE_INVALID_RESPONSE")


if __name__ == "__main__":
    unittest.main()
