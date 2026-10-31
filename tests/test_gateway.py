"""console.gateway 的单元测试：JWT 自签、端口解析、API 收敛、资源清单与推理档位探测。

全部用 ``httpx.MockTransport``，不触碰真实 AstrBot。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx

from console import gateway as gateway_module
from console.errors import ConsoleError
from console.gateway import Gateway, profile_identity

_ENV_KEYS = (
    "DASHBOARD_PORT",
    "ASTRBOT_DASHBOARD_PORT",
    "ASTRBOT_DATA",
    "MODEL_CONSOLE_STATE",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOME",
    "XDG_STATE_HOME",
)


def _b64decode(value: str) -> bytes:
    """base64url 解码（补回填充）。"""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _decode_jwt(token: str) -> tuple[dict, dict]:
    """拆解 JWT 的 header 与 payload。"""
    header, payload, _signature = token.split(".")
    return json.loads(_b64decode(header)), json.loads(_b64decode(payload))


def _verify_jwt(token: str, secret: str) -> bool:
    """用给定 secret 重算 HS256 签名并比对。"""
    header, payload, signature = token.split(".")
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(expected, _b64decode(signature))


def _json_response(payload: object, status: int = 200) -> httpx.Response:
    """构造 Dashboard 风格响应。"""
    return httpx.Response(status, json=payload)


class _GatewayCase(unittest.IsolatedAsyncioTestCase):
    """带临时 data 目录与 MockTransport 的测试基类。"""

    secret = "unit-test-secret"
    username = "astrbot"

    async def asyncSetUp(self) -> None:
        filtered = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS}
        self._env = patch.dict(os.environ, filtered, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "config").mkdir(parents=True)
        self.requests: list[httpx.Request] = []
        self._gateways: list[Gateway] = []

    async def asyncTearDown(self) -> None:
        for client in self._gateways:
            await client.close()

    def write_data(
        self,
        *,
        port: int | None = 6185,
        log_port: int | None = None,
        jwt_secret: str | None = None,
        username: str | None = None,
    ) -> None:
        """写入 ``cmd_config.json``（可选日志文件）。"""
        dashboard: dict = {
            "jwt_secret": self.secret if jwt_secret is None else jwt_secret,
            "username": self.username if username is None else username,
        }
        if port is not None:
            dashboard["port"] = port
        (self.data / "cmd_config.json").write_text(
            json.dumps({"dashboard": dashboard}), encoding="utf-8"
        )
        if log_port is not None:
            logs = self.data / "logs"
            logs.mkdir(exist_ok=True)
            (logs / "astrbot.log").write_text(
                "booting\nStarting WebUI at http://127.0.0.1:%d\n" % log_port,
                encoding="utf-8",
            )

    def make_gateway(self, handler) -> Gateway:
        """创建带 MockTransport 的网关。"""
        client = Gateway(
            {},
            self.data,
            transport=httpx.MockTransport(handler),
        )
        self._gateways.append(client)
        return client

    def dashboard_ok(self, data: object) -> httpx.Response:
        """构造 ``{status:ok,data}`` 响应。"""
        return _json_response({"status": "ok", "data": data})


class AuthAndReadTest(_GatewayCase):
    """JWT 与三类资源的读取。"""

    async def test_provider_read_signs_jwt(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.dashboard_ok({"provider": {"id": "p1", "model": "m1"}})

        client = self.make_gateway(handler)
        result = await client.read({"kind": "provider", "id": "p1"})

        self.assertEqual(result, {"id": "p1", "model": "m1"})
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/api/v1/providers/by-id")
        self.assertEqual(request.url.params["provider_id"], "p1")
        self.assertEqual(request.url.port, 6185)
        authorization = request.headers["Authorization"]
        self.assertTrue(authorization.startswith("Bearer "))
        token = authorization.removeprefix("Bearer ")
        header, payload = _decode_jwt(token)
        self.assertEqual(header, {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(payload, {"username": "astrbot"})
        self.assertTrue(_verify_jwt(token, self.secret))

    async def test_profile_and_plugin_read(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/api/v1/config-profiles/default":
                return self.dashboard_ok({"config": {"config_version": "1.0"}})
            if request.url.path == "/api/v1/plugins/config":
                self.assertEqual(request.url.params["plugin_id"], "meme_manager")
                return self.dashboard_ok({"config": {"semantic": {}}})
            return _json_response({"status": "error"}, status=404)

        client = self.make_gateway(handler)
        profile = await client.read({"kind": "profile", "id": "default"})
        plugin = await client.read({"kind": "plugin", "id": "meme_manager"})
        self.assertEqual(profile, {"config_version": "1.0"})
        self.assertEqual(plugin, {"semantic": {}})

    async def test_invalid_resource_kind(self) -> None:
        self.write_data()
        client = self.make_gateway(lambda request: self.dashboard_ok({}))
        with self.assertRaises(ConsoleError) as caught:
            await client.read({"kind": "device", "id": "x"})
        self.assertEqual(caught.exception.code, "INVALID_RESOURCE")

    async def test_invalid_api_path(self) -> None:
        client = self.make_gateway(lambda request: self.dashboard_ok({}))
        with self.assertRaises(ConsoleError) as caught:
            await client.call("GET", "/health")
        self.assertEqual(caught.exception.code, "INVALID_API_PATH")


class WriteTest(_GatewayCase):
    """三类资源的写入请求体。"""

    async def test_provider_write_body(self) -> None:
        self.write_data()
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return self.dashboard_ok({})

        client = self.make_gateway(handler)
        await client.write({"kind": "provider", "id": "p1"}, {"id": "p1", "model": "m2"})
        request = captured[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.url.path, "/api/v1/providers/by-id")
        body = json.loads(request.content)
        self.assertEqual(body, {"provider_id": "p1", "config": {"id": "p1", "model": "m2"}})

    async def test_profile_write_sends_config_directly(self) -> None:
        self.write_data()
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return self.dashboard_ok({})

        client = self.make_gateway(handler)
        await client.write({"kind": "profile", "id": "abc"}, {"config_version": "2"})
        request = captured[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.url.path, "/api/v1/config-profiles/abc")
        self.assertEqual(json.loads(request.content), {"config_version": "2"})

    async def test_plugin_write_body(self) -> None:
        self.write_data()
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return self.dashboard_ok({})

        client = self.make_gateway(handler)
        await client.write({"kind": "plugin", "id": "meme_manager"}, {"a": 1})
        body = json.loads(captured[0].content)
        self.assertEqual(body, {"plugin_id": "meme_manager", "config": {"a": 1}})

    async def test_read_only_rejects_write(self) -> None:
        self.write_data()
        client = self.make_gateway(lambda request: self.dashboard_ok({}))
        client.set_read_only(True)
        with self.assertRaises(ConsoleError) as caught:
            await client.write({"kind": "profile", "id": "default"}, {})
        self.assertEqual(caught.exception.code, "READ_ONLY_MODE")


class ErrorMappingTest(_GatewayCase):
    """上游失败 → 稳定错误码。"""

    async def test_http_error(self) -> None:
        self.write_data()
        client = self.make_gateway(lambda request: _json_response({}, status=500))
        with self.assertRaises(ConsoleError) as caught:
            await client.call("GET", "/api/v1/bots")
        self.assertEqual(caught.exception.code, "ASTRBOT_HTTP_500")

    async def test_invalid_json(self) -> None:
        self.write_data()
        client = self.make_gateway(lambda request: httpx.Response(200, text="<html>"))
        with self.assertRaises(ConsoleError) as caught:
            await client.call("GET", "/api/v1/bots")
        self.assertEqual(caught.exception.code, "ASTRBOT_INVALID_RESPONSE")

    async def test_api_rejected(self) -> None:
        self.write_data()
        client = self.make_gateway(lambda request: _json_response({"status": "error"}))
        with self.assertRaises(ConsoleError) as caught:
            await client.call("GET", "/api/v1/bots")
        self.assertEqual(caught.exception.code, "ASTRBOT_API_REJECTED")

    async def test_unreachable(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = self.make_gateway(handler)
        with self.assertRaises(ConsoleError) as caught:
            await client.call("GET", "/api/v1/bots")
        self.assertEqual(caught.exception.code, "ASTRBOT_UNREACHABLE")


class PortResolutionTest(_GatewayCase):
    """端口解析顺序：环境变量 → 配置 → 日志。"""

    async def test_config_port(self) -> None:
        self.write_data(port=6185, log_port=7001)
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.port)
            return self.dashboard_ok({"bots": []})

        client = self.make_gateway(handler)
        await client.call("GET", "/api/v1/bots")
        self.assertEqual(seen, [6185])

    async def test_log_port_fallback(self) -> None:
        self.write_data(port=None, log_port=7001)
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.port)
            return self.dashboard_ok({"bots": []})

        client = self.make_gateway(handler)
        await client.call("GET", "/api/v1/bots")
        self.assertEqual(seen, [7001])

    async def test_env_port_wins(self) -> None:
        self.write_data(port=6185, log_port=7001)
        os.environ["DASHBOARD_PORT"] = "6100"
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.port)
            return self.dashboard_ok({"bots": []})

        client = self.make_gateway(handler)
        await client.call("GET", "/api/v1/bots")
        self.assertEqual(seen, [6100])

    async def test_astrbot_dashboard_port_used(self) -> None:
        self.write_data(port=6185)
        os.environ["ASTRBOT_DASHBOARD_PORT"] = "6101"
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.port)
            return self.dashboard_ok({"bots": []})

        client = self.make_gateway(handler)
        await client.call("GET", "/api/v1/bots")
        self.assertEqual(seen, [6101])

    async def test_dashboard_not_found(self) -> None:
        self.write_data(port=None, log_port=None)
        client = self.make_gateway(lambda request: self.dashboard_ok({}))
        with self.assertRaises(ConsoleError) as caught:
            await client.call("GET", "/api/v1/bots")
        self.assertEqual(caught.exception.code, "DASHBOARD_NOT_FOUND")


class CachingTest(_GatewayCase):
    """配置文件只读一次（size+mtime 缓存）。"""

    async def test_config_read_cached(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            return self.dashboard_ok({"bots": []})

        client = self.make_gateway(handler)
        for _ in range(3):
            await client.call("GET", "/api/v1/bots")
        self.assertEqual(client.reads["config"], 1)
        client.invalidate()
        await client.call("GET", "/api/v1/bots")
        self.assertEqual(client.reads["config"], 2)


class EffortsTest(_GatewayCase):
    """推理档位探测只信任本机 http 服务。"""

    async def test_local_probe(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/providers/by-id":
                return self.dashboard_ok(
                    {
                        "provider": {
                            "id": "p1",
                            "model": "m1",
                            "api_base": "http://127.0.0.1:11434/v1/",
                            "key": ["sk"],
                        }
                    }
                )
            self.assertTrue(request.url.path.endswith("/v1/models"))
            self.assertEqual(request.url.params["client_version"], "0.161.0")
            self.assertEqual(request.headers["Authorization"], "Bearer sk")
            return _json_response(
                {
                    "data": [
                        {
                            "id": "m1",
                            "supported_reasoning_levels": [
                                "low",
                                {"effort": "high"},
                            ],
                        }
                    ]
                }
            )

        client = self.make_gateway(handler)
        levels = await client.efforts("p1")
        self.assertEqual(levels, ["low", "high"])

    async def test_remote_api_base_returns_none(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            return self.dashboard_ok(
                {"provider": {"id": "p1", "model": "m1", "api_base": "https://api.example.com/v1"}}
            )

        client = self.make_gateway(handler)
        self.assertIsNone(await client.efforts("p1"))

    async def test_probe_failure_returns_none(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/providers/by-id":
                return self.dashboard_ok(
                    {
                        "provider": {
                            "id": "p1",
                            "model": "m1",
                            "api_base": "http://127.0.0.1:11434",
                        }
                    }
                )
            return _json_response({}, status=500)

        client = self.make_gateway(handler)
        self.assertIsNone(await client.efforts("p1"))


class ResourcesTest(_GatewayCase):
    """资源清单与身份推导。"""

    async def test_resources_with_profiles_bots_and_plugins(self) -> None:
        self.write_data()
        (self.data / "config" / "meme_manager_config.json").write_text("{}", encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/config-profiles":
                return self.dashboard_ok(
                    {"info_list": [{"id": "default"}, {"id": "p2", "name": "小号"}]}
                )
            if path == "/api/v1/config-profiles/default":
                return self.dashboard_ok(
                    {"config": {"platform": [{"id": "qq1", "type": "aiocqhttp"}]}}
                )
            if path == "/api/v1/config-routes":
                return self.dashboard_ok({"routing": {"qq1::": "p2"}})
            if path == "/api/v1/bots":
                return self.dashboard_ok({"bots": [{"id": "qq1", "type": "aiocqhttp"}]})
            if path == "/api/v1/bots/stats":
                return self.dashboard_ok({"platforms": [{"id": "qq1", "status": "running"}]})
            return _json_response({"status": "error"}, status=404)

        client = self.make_gateway(handler)
        resources = await client.resources()

        self.assertEqual(
            [(item["kind"], item["id"]) for item in resources],
            [
                ("profile", "default"),
                ("profile", "p2"),
                ("plugin", "meme_manager"),
            ],
        )
        default_identity = resources[0]["identity"]
        self.assertEqual(default_identity["name"], "全局默认配置")
        self.assertEqual(default_identity["association"], "default")
        self.assertEqual(default_identity["instances"], [])
        linked_identity = resources[1]["identity"]
        self.assertEqual(linked_identity["association"], "platform-route")
        self.assertEqual(linked_identity["name"], "QQ机器人（OneBot）")
        self.assertEqual(linked_identity["instances"][0]["status"], "running")

        summary = await client.routing_summary()
        self.assertEqual(summary, {"total": 1, "platformLevel": 1, "granular": 0})

    async def test_routing_summary_never_raises(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client = self.make_gateway(handler)
        self.assertEqual(
            await client.routing_summary(),
            {"total": 0, "platformLevel": 0, "granular": 0},
        )

    async def test_plugins_inventory(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            return self.dashboard_ok(
                [
                    {
                        "name": "astrbot_plugin_model_websearch",
                        "display_name": "原生搜索",
                        "version": "v1.2.0",
                        "activated": True,
                    },
                    {"id": "other", "activated": False},
                    {"nope": True},
                ]
            )

        client = self.make_gateway(handler)
        plugins = await client.plugins()
        assert plugins is not None
        self.assertEqual(len(plugins), 2)
        self.assertEqual(plugins[0]["name"], "astrbot_plugin_model_websearch")
        self.assertEqual(plugins[0]["displayName"], "原生搜索")
        self.assertEqual(plugins[0]["version"], "v1.2.0")
        self.assertTrue(plugins[0]["activated"])
        self.assertEqual(plugins[1]["name"], "other")
        self.assertIsNone(plugins[1]["displayName"])

    async def test_plugins_unreadable_returns_none(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({}, status=500)

        client = self.make_gateway(handler)
        self.assertIsNone(await client.plugins())


class IdentityTest(unittest.TestCase):
    """profile_identity 的纯函数行为。"""

    def test_default_profile_with_routed_and_unrouted_platforms(self) -> None:
        identity = profile_identity(
            {"id": "default"},
            [
                {"id": "qq1", "type": "aiocqhttp", "status": "running"},
                {"id": "qq2", "type": "qq_official"},
            ],
            {"qq1::": "p2"},
        )
        self.assertEqual(identity["name"], "全局默认配置")
        self.assertEqual([item["instanceId"] for item in identity["instances"]], ["qq2"])
        self.assertEqual(identity["instances"][0]["category"], "QQ机器人")

    def test_unassigned_profile(self) -> None:
        identity = profile_identity({"id": "abcdef123456"}, [], {})
        self.assertEqual(identity["association"], "unassigned")
        self.assertTrue(identity["name"].startswith("未关联配置"))

    def test_profile_without_id(self) -> None:
        identity = profile_identity({"uuid": "u1"}, [], {})
        self.assertEqual(identity["association"], "unassigned")


class AdminSurfaceTest(_GatewayCase):
    """其它契约方法的返回结构。"""

    async def test_profiles_providers_routes(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/config-profiles":
                return self.dashboard_ok({"info_list": []})
            if path == "/api/v1/providers":
                return self.dashboard_ok({"providers": []})
            if path == "/api/v1/config-routes":
                return self.dashboard_ok({"routing": {"qq1::": "default"}})
            return _json_response({"status": "error"}, status=404)

        client = self.make_gateway(handler)
        self.assertEqual(await client.profiles(), {"info_list": []})
        self.assertEqual(await client.providers(), {"providers": []})
        self.assertEqual(await client.routes(), {"qq1::": "default"})

    async def test_bots_tuple(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/bots":
                return self.dashboard_ok({"bots": [{"id": "qq1"}]})
            return self.dashboard_ok({"platforms": [{"id": "qq1", "status": "running"}]})

        client = self.make_gateway(handler)
        bots, stats = await client.bots()
        self.assertEqual(bots, [{"id": "qq1"}])
        self.assertEqual(stats, [{"id": "qq1", "status": "running"}])

    async def test_plugin_settings(self) -> None:
        self.write_data()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/plugins":
                return self.dashboard_ok([{"name": "a"}, {"name": "b"}])
            plugin_id = request.url.params["plugin_id"]
            if plugin_id == "a":
                return self.dashboard_ok({"config": {"x": 1}})
            return _json_response({}, status=500)

        client = self.make_gateway(handler)
        settings = await client.plugin_settings()
        self.assertEqual(settings, {"a": {"x": 1}})

    def test_signature_matches_hmac(self) -> None:
        token = Gateway._sign("secret", "astrbot")
        self.assertTrue(_verify_jwt(token, "secret"))
        self.assertFalse(_verify_jwt(token, "other"))
        _header, payload = _decode_jwt(token)
        self.assertEqual(payload, {"username": "astrbot"})


if __name__ == "__main__":
    unittest.main()
