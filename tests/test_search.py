"""搜索链路测试模块（``search_key_test`` / ``native_search_test``）单元测试。

全部用 ``httpx.MockTransport`` 拦截，**不发真实网络请求、不打真实模型接口**；
重点验证：固定 https 主机、Key 不泄露、状态码/业务错误映射、超时与时间口径。
"""

from __future__ import annotations

import json
import unittest

import httpx

from console.native_search_test import provider_source, test_native_search
from console.search_key_test import (
    SUPPORTED_PROVIDERS,
    SPECS,
    safe_status_message,
    test_search_key,
)

KEY = "sk-test-SECRET-123456"

CONFIG = {
    "provider": [{"id": "example-provider/model-a",
                  "provider_source_id": "example-provider", "model": "model-a"}],
    "provider_sources": [{"id": "example-provider",
                          "type": "openai_chat_completion",
                          "api_base": "http://127.0.0.1:8080/v1",
                          "key": ["secret-key"]}],
}


def make_client(handler) -> httpx.AsyncClient:
    """用 MockTransport 建一个可注入客户端。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_response(status: int, body: object) -> httpx.Response:
    """JSON 响应。"""
    return httpx.Response(status, json=body)


class SearchKeyTests(unittest.IsolatedAsyncioTestCase):
    """9 个内置搜索提供商的单 Key 探测。"""

    async def test_all_nine_builtin_providers_are_supported(self) -> None:
        self.assertEqual(sorted(SUPPORTED_PROVIDERS), [
            "anysearch", "baidu_ai_search", "bocha", "brave", "deepseek",
            "exa", "firecrawl", "serpapi", "tavily",
        ])
        self.assertEqual(SUPPORTED_PROVIDERS, list(SPECS))

    async def test_each_provider_hits_fixed_https_host_without_leaking_key(self) -> None:
        for provider in SUPPORTED_PROVIDERS:
            with self.subTest(provider=provider):
                calls: list[httpx.Request] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request)
                    return json_response(200, {"results": []})

                client = make_client(handler)
                try:
                    result = await test_search_key(provider, KEY, client=client)
                finally:
                    await client.aclose()
                self.assertTrue(result["ok"], provider)
                self.assertEqual(result["provider"], provider)
                self.assertTrue(result["label"])
                self.assertEqual(result["status"], 200)
                self.assertIsInstance(result["latencyMs"], int)
                self.assertEqual(len(calls), 1)
                self.assertTrue(str(calls[0].url).startswith("https://"), provider)
                self.assertIn(calls[0].method, ("GET", "POST"))
                serialized = json.dumps(result)
                self.assertNotIn(KEY, serialized)
                self.assertNotIn("SECRET", serialized)
                self.assertTrue(calls[0].headers.get("Authorization")
                                or calls[0].headers.get("x-api-key")
                                or calls[0].headers.get("X-Subscription-Token")
                                or "api_key=" in str(calls[0].url),
                                f"{provider} must carry the key somewhere reviewed")

    async def test_auth_quota_and_server_failures_are_mapped(self) -> None:
        cases = [
            (401, "鉴权失败"), (403, "鉴权失败"), (429, "频率"),
            (500, "暂时不可用"), (404, "请求被拒绝"),
        ]
        for status, pattern in cases:
            with self.subTest(status=status):
                client = make_client(lambda _r, s=status: json_response(
                    s, {"error": f"API key {KEY}"}))
                try:
                    result = await test_search_key("deepseek", KEY, client=client)
                finally:
                    await client.aclose()
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertIn(pattern, result["message"])
                self.assertNotIn(KEY, json.dumps(result))

    async def test_provider_business_errors_are_failures(self) -> None:
        client = make_client(lambda _r: json_response(200, {"code": 7,
                                                            "message": "bad"}))
        try:
            anysearch = await test_search_key("anysearch", KEY, client=client)
        finally:
            await client.aclose()
        self.assertFalse(anysearch["ok"])
        self.assertIn("业务错误", anysearch["message"])

        client = make_client(lambda _r: json_response(200, {
            "search_metadata": {"status": "Error"}, "error": "invalid key"}))
        try:
            serpapi = await test_search_key("serpapi", KEY, client=client)
        finally:
            await client.aclose()
        self.assertFalse(serpapi["ok"])
        self.assertIn("SerpApi", serpapi["message"])

    async def test_anysearch_zero_code_and_deepseek_result_count(self) -> None:
        client = make_client(lambda _r: json_response(200, {"code": 0}))
        try:
            ok = await test_search_key("anysearch", KEY, client=client)
        finally:
            await client.aclose()
        self.assertTrue(ok["ok"])

        client = make_client(lambda _r: json_response(
            200, {"results": [{"title": "a"}, {"title": "b"}]}))
        try:
            deepseek = await test_search_key("deepseek", KEY, client=client)
        finally:
            await client.aclose()
        self.assertTrue(deepseek["ok"])
        self.assertIn("返回 2 条结果", deepseek["message"])

    async def test_deepseek_specific_auth_and_balance_hints(self) -> None:
        client = make_client(lambda _r: json_response(401, {}))
        try:
            auth = await test_search_key("deepseek", KEY, client=client)
        finally:
            await client.aclose()
        self.assertIn("platform.deepseek.com", auth["message"])
        client = make_client(lambda _r: json_response(402, {}))
        try:
            balance = await test_search_key("deepseek", KEY, client=client)
        finally:
            await client.aclose()
        self.assertIn("余额不足", balance["message"])

    async def test_unknown_provider_and_missing_key_never_request(self) -> None:
        called = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called += 1
            return json_response(200, {})

        client = make_client(handler)
        try:
            unknown = await test_search_key("nope", KEY, client=client)
            empty = await test_search_key("deepseek", "", client=client)
            missing = await test_search_key("deepseek", None, client=client)  # type: ignore[arg-type]
        finally:
            await client.aclose()
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["message"], "不支持的搜索提供商。")
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["message"], "没有可测试的 Key：请先填写，或先保存 Key。")
        self.assertFalse(missing["ok"])
        self.assertEqual(called, 0)

    async def test_timeouts_are_reported_as_timeout(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        client = make_client(handler)
        try:
            result = await test_search_key("tavily", KEY, client=client,
                                           timeout_ms=1000)
        finally:
            await client.aclose()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertIn("超时", result["message"])

    async def test_network_errors_are_reported_without_bodies(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("ECONNREFUSED")

        client = make_client(handler)
        try:
            result = await test_search_key("brave", KEY, client=client)
        finally:
            await client.aclose()
        self.assertFalse(result["ok"])
        self.assertIn("网络", result["message"])

    def test_safe_status_message_is_key_free(self) -> None:
        for status in (400, 401, 403, 402, 429, 500, 503):
            message = safe_status_message(status, "tavily")
            self.assertNotIn(KEY, message)
            self.assertIn(str(status), message)


class NativeSearchTests(unittest.IsolatedAsyncioTestCase):
    """原生搜索探测（OpenAI Responses + Gemini 扩展分支）。"""

    async def test_provider_source_resolution(self) -> None:
        resolved = provider_source(CONFIG, "example-provider/model-a")
        assert resolved is not None
        self.assertEqual(resolved["model"], "model-a")
        self.assertEqual(resolved["api_base"], "http://127.0.0.1:8080/v1")
        self.assertEqual(resolved["key"], "secret-key")
        self.assertIsNone(provider_source(CONFIG, "missing"))
        self.assertIsNone(provider_source({}, "x"))
        # provider 存在但 provider_source 缺失时，Node 返回一个空 source（key/api_base 为空），
        # 由 test_native_search 报「缺少 api_base 或 Key」，不在这里返回 None。
        broken = provider_source(
            {"provider": [{"id": "x", "provider_source_id": "missing", "model": "m"}],
             "provider_sources": []}, "x")
        assert broken is not None
        self.assertEqual(broken["api_base"], "")
        self.assertIsNone(broken["key"])

    async def test_detects_web_search_call_and_citations(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return json_response(200, {"output": [
                {"type": "web_search_call", "action": {"queries": ["q"]}},
                {"type": "message", "content": [
                    {"type": "output_text", "text": "answer", "annotations": [
                        {"type": "url_citation", "url": "https://example.com/1"}]},
                ]},
            ]})

        client = make_client(handler)
        try:
            result = await test_native_search(CONFIG, "example-provider/model-a",
                                              client=client)
        finally:
            await client.aclose()
        self.assertTrue(result["ok"])
        self.assertEqual(result["searchCalls"], 1)
        self.assertEqual(result["citations"], 1)
        self.assertEqual(sorted(result), ["citations", "latencyMs", "message",
                                          "model", "ok", "provider", "searchCalls",
                                          "status"])
        self.assertTrue(str(captured[0].url).endswith("/v1/responses"))
        body = json.loads(captured[0].content)
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["max_output_tokens"], 800)
        self.assertFalse(body["store"])
        self.assertNotIn("secret-key", json.dumps(result))

    async def test_model_without_actual_search_is_unsupported(self) -> None:
        client = make_client(lambda _r: json_response(200, {"output": [
            {"type": "message", "content": [
                {"type": "output_text", "text": "cannot browse", "annotations": []}]},
        ]}))
        try:
            result = await test_native_search(CONFIG, "example-provider/model-a",
                                              client=client)
        finally:
            await client.aclose()
        self.assertFalse(result["ok"])
        self.assertIn("没有实际执行 web_search", result["message"])

    async def test_http_failures_are_mapped_without_key(self) -> None:
        client = make_client(lambda _r: json_response(
            401, {"error": "bad key secret-key"}))
        try:
            result = await test_native_search(CONFIG, "example-provider/model-a",
                                              client=client)
        finally:
            await client.aclose()
        self.assertFalse(result["ok"])
        self.assertIn("鉴权失败", result["message"])
        self.assertNotIn("secret-key", json.dumps(result))

    async def test_unknown_provider_and_missing_config_never_request(self) -> None:
        called = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called += 1
            return json_response(200, {})

        client = make_client(handler)
        try:
            unknown = await test_native_search(CONFIG, "nope", client=client)
            broken = await test_native_search(
                {"provider": [{"id": "x", "provider_source_id": "missing",
                               "model": "m"}], "provider_sources": []},
                "x", client=client)
            no_key = await test_native_search(
                {"provider": [{"id": "x", "provider_source_id": "s",
                               "model": "m"}],
                 "provider_sources": [{"id": "s", "api_base": "http://h:1",
                                       "key": []}]},
                "x", client=client)
        finally:
            await client.aclose()
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["message"], "找不到该模型 Provider。")
        self.assertFalse(broken["ok"])
        self.assertFalse(no_key["ok"])
        self.assertIn("缺少 api_base 或 Key", no_key["message"])
        self.assertEqual(called, 0)

    async def test_timeouts_are_reported(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        client = make_client(handler)
        try:
            result = await test_native_search(CONFIG, "example-provider/model-a",
                                              client=client, timeout_ms=5000)
        finally:
            await client.aclose()
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["message"])

    async def test_gemini_provider_uses_native_google_search(self) -> None:
        config = {
            "provider": [{"id": "g", "provider_source_id": "gs",
                          "model": "gemini-2.0-flash"}],
            "provider_sources": [{"id": "gs", "type": "gemini",
                                  "api_base": "http://127.0.0.1:8080/v1",
                                  "key": ["gemini-key"]}],
        }
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            self.assertIn(":generateContent", request.url.path)
            return json_response(200, {"candidates": [{
                "groundingMetadata": {
                    "webSearchQueries": ["q1", "q2"],
                    "groundingChunks": [{"web": {"uri": "https://a"}},
                                        {"web": {"uri": "https://b"}}],
                    "groundingSupports": [{"segment": {}}],
                },
            }]})

        client = make_client(handler)
        try:
            result = await test_native_search(config, "g", client=client)
        finally:
            await client.aclose()
        self.assertTrue(result["ok"])
        self.assertEqual(result["searchCalls"], 2)
        self.assertEqual(result["citations"], 2)
        self.assertIn("google_search", result["message"])
        self.assertEqual(len(captured), 1)
        body = json.loads(captured[0].content)
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertNotIn("gemini-key", json.dumps(result))

    async def test_gemini_failure_falls_back_to_responses(self) -> None:
        config = {
            "provider": [{"id": "g", "provider_source_id": "gs",
                          "model": "gemini-2.0-flash"}],
            "provider_sources": [{"id": "gs", "type": "gemini",
                                  "api_base": "http://127.0.0.1:8080/v1",
                                  "key": ["gemini-key"]}],
        }
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if ":generateContent" in request.url.path:
                return json_response(404, {"error": "no native layer"})
            return json_response(200, {"output": [
                {"type": "web_search_call"},
                {"type": "message", "content": [
                    {"annotations": [{"type": "url_citation",
                                      "url": "https://example.com"}]}]},
            ]})

        client = make_client(handler)
        try:
            result = await test_native_search(config, "g", client=client)
        finally:
            await client.aclose()
        self.assertTrue(result["ok"], "fallback Responses must be used")
        self.assertEqual(result["searchCalls"], 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("回退 Responses", result["message"])
        self.assertIn("Gemini 原生搜索不可用", result["message"])

    async def test_gemini_and_responses_both_fail_keeps_fallback_note(self) -> None:
        config = {
            "provider": [{"id": "g", "provider_source_id": "gs",
                          "model": "gemini-2.0-flash"}],
            "provider_sources": [{"id": "gs", "type": "gemini",
                                  "api_base": "http://127.0.0.1:8080/v1",
                                  "key": ["gemini-key"]}],
        }
        client = make_client(lambda _r: json_response(500, {"error": "down"}))
        try:
            result = await test_native_search(config, "g", client=client)
        finally:
            await client.aclose()
        self.assertFalse(result["ok"])
        self.assertIn("回退 Responses", result["message"])
        self.assertEqual(result["status"], 500)


if __name__ == "__main__":
    unittest.main()
