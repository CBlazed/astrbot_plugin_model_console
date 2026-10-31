"""AstrBot 内置网页搜索提供商的「单 Key 真实测试」。

对齐旧 Node 实现 ``model-console/src/search-key-test.mjs``：

* 9 个内置提供商，请求只发往固定、已审查的 https 主机；
* Key 只出现在请求头/请求体里，**绝不出现在结果、错误信息或日志中**；
* 返回 ``{ok,provider,label,status,latencyMs,message}``，字段名与旧实现逐字一致；
* 支持 ``anysearch`` / ``serpapi`` 的业务错误识别，``deepseek`` 成功时报告结果条数。

本模块只做单次探测；限速窗口由 app 层维护（契约 §7）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

_TIMEOUT_MS = 15000


def _tavily(key: str) -> dict[str, Any]:
    """Tavily 请求定义。"""
    return {
        "url": "https://api.tavily.com/search",
        "method": "POST",
        "headers": {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "body": json.dumps({"query": "AstrBot", "max_results": 1}),
    }


def _bocha(key: str) -> dict[str, Any]:
    """Bocha 请求定义。"""
    return {
        "url": "https://api.bochaai.com/v1/web-search",
        "method": "POST",
        "headers": {"Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "gzip, deflate"},
        "body": json.dumps({"query": "AstrBot", "count": 1}),
    }


def _brave(key: str) -> dict[str, Any]:
    """Brave 请求定义。"""
    return {
        "url": "https://api.search.brave.com/res/v1/web/search?q=AstrBot&count=1",
        "method": "GET",
        "headers": {"Accept": "application/json", "X-Subscription-Token": key},
        "body": None,
    }


def _serpapi(key: str) -> dict[str, Any]:
    """SerpApi 请求定义。"""
    from urllib.parse import quote

    return {
        "url": (f"https://serpapi.com/search.json?engine=google&q=AstrBot"
                f"&api_key={quote(key, safe='')}"),
        "method": "GET",
        "headers": {"Accept": "application/json"},
        "body": None,
    }


def _firecrawl(key: str) -> dict[str, Any]:
    """Firecrawl 请求定义。"""
    return {
        "url": "https://api.firecrawl.dev/v2/search",
        "method": "POST",
        "headers": {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "body": json.dumps({"query": "AstrBot", "limit": 1}),
    }


def _exa(key: str) -> dict[str, Any]:
    """Exa 请求定义。"""
    return {
        "url": "https://api.exa.ai/search",
        "method": "POST",
        "headers": {"x-api-key": key, "Content-Type": "application/json"},
        "body": json.dumps({"query": "AstrBot", "numResults": 1}),
    }


def _anysearch(key: str) -> dict[str, Any]:
    """AnySearch 请求定义（无 Key 时也允许匿名请求）。"""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return {
        "url": "https://api.anysearch.com/v1/search",
        "method": "POST",
        "headers": headers,
        "body": json.dumps({"query": "AstrBot", "max_results": 1, "format": "json"}),
    }


def _deepseek(key: str) -> dict[str, Any]:
    """DeepSeek Search API 请求定义。"""
    return {
        "url": "https://api.deepseek.com/v1/tools/search",
        "method": "POST",
        "headers": {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "body": json.dumps({"query": "AstrBot", "count": 1, "need_content": False}),
    }


def _baidu_ai_search(key: str) -> dict[str, Any]:
    """百度 AI Search 请求定义。"""
    return {
        "url": "https://qianfan.baidubce.com/v2/ai_search/web_search",
        "method": "POST",
        "headers": {
            "Authorization": f"Bearer {key}",
            "X-Appbuilder-Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        "body": json.dumps({
            "messages": [{"role": "user", "content": "AstrBot"}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": 1}],
        }),
    }


#: 提供商名 → 定义；顺序与 AstrBot 4.29.3 内置列表一致（也是旧实现的键顺序）。
SPECS: dict[str, dict[str, Any]] = {
    "tavily": {"label": "Tavily", "request": _tavily},
    "bocha": {"label": "Bocha", "request": _bocha},
    "brave": {"label": "Brave", "request": _brave},
    "serpapi": {"label": "SerpApi", "request": _serpapi},
    "firecrawl": {"label": "Firecrawl", "request": _firecrawl},
    "exa": {"label": "Exa", "request": _exa},
    "anysearch": {"label": "AnySearch", "request": _anysearch},
    "deepseek": {"label": "DeepSeek Search API", "request": _deepseek},
    "baidu_ai_search": {"label": "Baidu AI Search", "request": _baidu_ai_search},
}

SUPPORTED_PROVIDERS: list[str] = list(SPECS)


def provider_label(provider: str) -> str:
    """返回提供商的展示名（未知提供商原样返回）。"""
    spec = SPECS.get(provider) if isinstance(provider, str) else None
    if spec:
        return str(spec["label"])
    return "" if provider is None else str(provider)


def _js_string(value: Any) -> str:
    """模拟 JS ``String(value)``（错误信息里会用到）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def safe_status_message(status: int, provider: str) -> str:
    """按 HTTP 状态码给可读且不含响应体的错误文案（旧实现 ``safeStatusMessage``）。"""
    if status in (401, 403):
        if provider == "deepseek":
            return (f"鉴权失败（HTTP {status}）：DeepSeek API Key 缺失或无效。"
                    "请在 platform.deepseek.com 检查/重新创建 Key（Search/Fetch 与模型共用同一个 Key）。")
        return f"鉴权失败（HTTP {status}）：Key 无效、过期或无权限。"
    if status == 402:
        if provider == "deepseek":
            return "余额不足（HTTP 402）：请先在 DeepSeek 平台充值后重试。"
        return "额度受限（HTTP 402）：请检查配额或充值。"
    if status == 429:
        return f"超过频率/QPS 限制（HTTP {status}）：请稍后重试。"
    if status >= 500:
        return f"搜索服务暂时不可用（HTTP {status}），请稍后重试。"
    return f"请求被拒绝（HTTP {status}），请检查 Key 与提供商配置。"


def _failure(provider: Any, label: str, *, status: int | None, latency_ms: int,
             message: str) -> dict[str, Any]:
    """构造失败结果（字段顺序与旧实现一致）。"""
    return {"ok": False, "provider": provider, "label": label, "status": status,
            "latencyMs": latency_ms, "message": message}


async def test_search_key(provider: str, key: str, *,
                          client: httpx.AsyncClient | None = None,
                          transport: httpx.AsyncBaseTransport | None = None,
                          timeout_ms: float = _TIMEOUT_MS) -> dict[str, Any]:
    """用一个最小真实搜索请求探测单个提供商 Key 是否可用。

    Args:
        provider: 提供商名（必须属于 :data:`SUPPORTED_PROVIDERS`）。
        key: 待测试的 API Key（可空；空值直接返回失败，不发请求）。
        client: 测试注入的 httpx 客户端。
        transport: 测试注入的 httpx transport（``httpx.MockTransport``）。
        timeout_ms: 单次请求超时（毫秒，最小 1000）。

    Returns:
        ``{ok,provider,label,status,latencyMs,message}``。
    """
    spec = SPECS.get(provider) if isinstance(provider, str) else None
    if not spec:
        return _failure("" if provider is None else provider, "", status=None,
                        latency_ms=0, message="不支持的搜索提供商。")
    api_key = key.strip() if isinstance(key, str) else ""
    if not api_key:
        return _failure(provider, str(spec["label"]), status=None, latency_ms=0,
                        message="没有可测试的 Key：请先填写，或先保存 Key。")
    request = spec["request"](api_key)
    seconds = max(1.0, float(timeout_ms) / 1000.0 if timeout_ms else 15.0)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=seconds, transport=transport,
                                   follow_redirects=False)
    started = time.monotonic()
    try:
        kwargs: dict[str, Any] = {"headers": request["headers"],
                                  "timeout": httpx.Timeout(seconds)}
        if request["body"] is not None:
            kwargs["content"] = request["body"].encode("utf-8")
        response = await client.request(request["method"], request["url"], **kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)
        status = int(response.status_code)
        if status < 200 or status >= 300:
            return _failure(provider, str(spec["label"]), status=status,
                            latency_ms=latency_ms,
                            message=safe_status_message(status, provider))
        text = response.text
        body: Any = None
        if text:
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        if (provider == "anysearch" and isinstance(body, dict)
                and body.get("code") is not None and body.get("code") != 0):
            return _failure(provider, str(spec["label"]), status=status,
                            latency_ms=latency_ms,
                            message=f"AnySearch 业务错误（code={_js_string(body.get('code'))}），"
                                    f"请检查 Key 或参数。")
        if provider == "serpapi" and isinstance(body, dict):
            metadata = body.get("search_metadata")
            meta_status = metadata.get("status") if isinstance(metadata, dict) else None
            if meta_status == "Error" or (body.get("error") and meta_status != "Success"):
                return _failure(provider, str(spec["label"]), status=status,
                                latency_ms=latency_ms,
                                message="SerpApi 返回业务错误，请检查 Key 或配额。")
        results = body.get("results") if isinstance(body, dict) else None
        if provider == "deepseek" and isinstance(results, list):
            return {"ok": True, "provider": provider, "label": str(spec["label"]),
                    "status": status, "latencyMs": latency_ms,
                    "message": f"连接成功，Key 可用（本次测试查询返回 {len(results)} 条结果）。"}
        return {"ok": True, "provider": provider, "label": str(spec["label"]),
                "status": status, "latencyMs": latency_ms,
                "message": "连接成功，Key 可用。"}
    except Exception as error:  # noqa: BLE001 - 网络/超时统一转可读失败
        latency_ms = int((time.monotonic() - started) * 1000)
        timeout = isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException))
        return _failure(provider, str(spec["label"]), status=None,
                        latency_ms=latency_ms,
                        message=("测试超时：请检查网络或稍后重试。"
                                 if timeout else
                                 "网络错误或请求失败：请检查本机网络与代理设置。"))
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
