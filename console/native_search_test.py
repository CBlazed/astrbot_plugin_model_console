"""模型原生网页搜索探测（真实发起一次搜索）。

对齐旧 Node 实现 ``model-console/src/native-search-test.mjs``：

* 默认走 OpenAI Responses API：``POST {api_base}/responses`` + ``tools=[{"type":"web_search"}]``，
  从 ``output`` 里的 ``web_search_call`` 与 ``url_citation`` 统计是否真的执行了搜索；
* 使用 profile 自带的 provider/provider_sources 凭据，Key 永远不离开进程；
* 返回字段与旧实现逐字一致：
  ``{ok,provider,model,status,latencyMs,searchCalls,citations,message}``。

**相对 Node 基线的扩展**：检测到 Gemini provider 时，先试 Gemini 原生
``POST {root}/v1beta/models/{model}:generateContent`` + ``tools=[{"type":"google_search"}]``；
原生失败（HTTP/超时/网络/无 grounding）**自动回落**到上面的 Responses 路径，并在 ``message``
末尾附上回落原因。非 Gemini provider 的代码路径与响应字段不受影响。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

_RESPONSES_INSTRUCTIONS = (
    "Use the web_search tool to find current information. "
    "Answer briefly and cite at least one URL."
)
_RESPONSES_INPUT = "What is a recent notable news item? Cite one source URL."
_GEMINI_INPUT = "What is a recent notable news item? Cite one source URL."
_NOT_SEARCHED = (
    "模型没有实际执行 web_search（可能不支持原生搜索；"
    "DeepSeek 官方目前仅 deepseek-v4-pro 支持 Responses web_search）。"
)
_GEMINI_NOT_SEARCHED = "模型没有实际执行 google_search（可能不支持 Gemini 原生搜索）。"
_TIMEOUT_MESSAGE = "测试超时：模型在限定时间内没有返回。"
_NETWORK_MESSAGE = "网络错误或请求失败：请检查 provider 地址与网络。"


def _result(*, ok: bool, provider: Any, model: Any, status: int | None,
            latency_ms: int, search_calls: int, citations: int,
            message: str) -> dict[str, Any]:
    """构造与旧实现同形的探测结果。"""
    return {"ok": ok, "provider": provider, "model": model, "status": status,
            "latencyMs": latency_ms, "searchCalls": search_calls,
            "citations": citations, "message": message}


def safe_error(status: int) -> str:
    """OpenAI Responses 路径的 HTTP 错误文案（旧实现 ``safeError``）。"""
    if status in (401, 403):
        return f"鉴权失败（HTTP {status}）：Key 无效、过期或无权限。"
    if status in (402, 429):
        return f"额度或频率受限（HTTP {status}）。"
    if status == 404:
        return "该 provider 不支持 Responses API（HTTP 404），无法使用模型原生搜索。"
    if status >= 500:
        return f"搜索服务暂时不可用（HTTP {status}）。"
    return f"请求被拒绝（HTTP {status}）。"


def _gemini_safe_error(status: int) -> str:
    """Gemini 原生路径的 HTTP 错误文案。"""
    if status in (401, 403):
        return f"Gemini 原生搜索鉴权失败（HTTP {status}）：Key 无效、过期或无权限。"
    if status in (402, 429):
        return f"Gemini 原生搜索额度或频率受限（HTTP {status}）。"
    if status == 404:
        return "该 provider 不支持 Gemini 原生 /v1beta generateContent（HTTP 404）。"
    if status >= 500:
        return f"Gemini 原生搜索服务暂时不可用（HTTP {status}）。"
    return f"Gemini 原生搜索请求被拒绝（HTTP {status}）。"


def provider_source(config: Mapping[str, Any], provider_id: str) -> dict[str, Any] | None:
    """解析 profile 配置里的 provider + provider_source（旧实现 ``providerSource``）。"""
    if not isinstance(config, Mapping):
        return None
    providers = config.get("provider")
    entry = None
    if isinstance(providers, list):
        entry = next((item for item in providers
                      if isinstance(item, Mapping) and item.get("id") == provider_id), None)
    if entry is None:
        return None
    sources = config.get("provider_sources")
    source: Mapping[str, Any] = {}
    if isinstance(sources, list):
        found = next((item for item in sources
                      if isinstance(item, Mapping)
                      and item.get("id") == entry.get("provider_source_id")), None)
        source = found or {}
    raw_key = source.get("key")
    if isinstance(raw_key, list):
        key = next((item for item in raw_key
                    if isinstance(item, str) and item), None)
    else:
        key = raw_key
    model = entry.get("model")
    if model is None:
        model = source.get("model")
    if model is None:
        model = ""
    api_base = str(source.get("api_base") or "")
    if api_base.endswith("/"):
        api_base = api_base[:-1]
    return {"entry": entry, "source": source, "key": key, "model": model,
            "api_base": api_base}


def _is_gemini(source: Mapping[str, Any], model: Any, api_base: str) -> bool:
    """判断是否 Gemini provider（type / 模型名 / 主机名任一命中）。"""
    source_type = source.get("type") if isinstance(source, Mapping) else None
    if isinstance(source_type, str) and "gemini" in source_type.lower():
        return True
    if isinstance(model, str) and "gemini" in model.lower():
        return True
    host = api_base.lower()
    return "generativelanguage.googleapis.com" in host


def _gemini_endpoint(api_base: str, model: str) -> str:
    """由 provider 的 api_base 拼出 Gemini 原生 generateContent 端点。"""
    root = api_base
    if root.endswith("/v1beta"):
        root = root[: -len("/v1beta")]
    elif root.endswith("/v1"):
        root = root[:-len("/v1")]
    encoded = quote(model, safe="")
    return f"{root}/v1beta/models/{encoded}:generateContent"


def _count_citations(output: Any) -> int:
    """统计 Responses output 里的 url 引用条数。"""
    count = 0
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, Mapping):
                continue
            annotations = part.get("annotations")
            for annotation in annotations if isinstance(annotations, list) else []:
                if not isinstance(annotation, Mapping):
                    continue
                if annotation.get("type") == "url_citation" or annotation.get("url"):
                    count += 1
    return count


def _gemini_counts(body: Any) -> tuple[int, int]:
    """从 Gemini generateContent 响应统计 (google_search 次数, grounding 引用条数)。"""
    search_calls = 0
    citations = 0
    candidates = body.get("candidates") if isinstance(body, Mapping) else None
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, Mapping):
            continue
        metadata = candidate.get("groundingMetadata")
        if not isinstance(metadata, Mapping):
            continue
        queries = metadata.get("webSearchQueries")
        if isinstance(queries, list):
            search_calls += len(queries)
        chunks = metadata.get("groundingChunks")
        supports = metadata.get("groundingSupports")
        if isinstance(chunks, list) and chunks:
            citations += len(chunks)
        elif isinstance(supports, list):
            citations += len(supports)
    return search_calls, citations


def _client_for(client: httpx.AsyncClient | None,
                transport: httpx.AsyncBaseTransport | None,
                seconds: float) -> tuple[httpx.AsyncClient, bool]:
    """返回 (client, 是否自建)；自建的由调用方释放。"""
    if client is not None:
        return client, False
    return (httpx.AsyncClient(timeout=seconds, transport=transport,
                              follow_redirects=False), True)


async def _run_responses(*, client: httpx.AsyncClient, provider_id: str, model: str,
                         api_base: str, key: str, seconds: float) -> dict[str, Any]:
    """OpenAI Responses ``web_search`` 探测（与 Node 基线逐字段一致）。"""
    started = time.monotonic()
    body = {
        "model": model,
        "instructions": _RESPONSES_INSTRUCTIONS,
        "input": _RESPONSES_INPUT,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "max_output_tokens": 800,
        "store": False,
    }
    try:
        response = await client.post(
            f"{api_base}/responses",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            content=json.dumps(body).encode("utf-8"),
            timeout=httpx.Timeout(seconds),
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        status = int(response.status_code)
        if status < 200 or status >= 300:
            return _result(ok=False, provider=provider_id, model=model, status=status,
                           latency_ms=latency_ms, search_calls=0, citations=0,
                           message=safe_error(status))
        try:
            payload = response.json()
        except ValueError:
            payload = None
        output = payload.get("output") if isinstance(payload, Mapping) else None
        output = output if isinstance(output, list) else []
        search_calls = sum(1 for item in output
                           if isinstance(item, Mapping) and item.get("type") == "web_search_call")
        citations = _count_citations(output)
        if search_calls == 0 and citations == 0:
            return _result(ok=False, provider=provider_id, model=model, status=status,
                           latency_ms=latency_ms, search_calls=search_calls,
                           citations=citations, message=_NOT_SEARCHED)
        return _result(ok=True, provider=provider_id, model=model, status=status,
                       latency_ms=latency_ms, search_calls=search_calls,
                       citations=citations,
                       message=f"模型原生搜索可用（web_search_call {search_calls} 次，"
                               f"引用 {citations} 条）。")
    except Exception as error:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        timeout = isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException))
        return _result(ok=False, provider=provider_id, model=model, status=None,
                       latency_ms=latency_ms, search_calls=0, citations=0,
                       message=_TIMEOUT_MESSAGE if timeout else _NETWORK_MESSAGE)


async def _run_gemini(*, client: httpx.AsyncClient, provider_id: str, model: str,
                      api_base: str, key: str, seconds: float) -> dict[str, Any]:
    """Gemini 原生 ``google_search`` 探测（相对 Node 基线的扩展分支）。"""
    started = time.monotonic()
    payload = {
        "contents": [{"parts": [{"text": _GEMINI_INPUT}]}],
        "tools": [{"google_search": {}}],
    }
    try:
        response = await client.post(
            _gemini_endpoint(api_base, model),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            content=json.dumps(payload).encode("utf-8"),
            timeout=httpx.Timeout(seconds),
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        status = int(response.status_code)
        if status < 200 or status >= 300:
            return _result(ok=False, provider=provider_id, model=model, status=status,
                           latency_ms=latency_ms, search_calls=0, citations=0,
                           message=_gemini_safe_error(status))
        try:
            body = response.json()
        except ValueError:
            body = None
        search_calls, citations = _gemini_counts(body)
        if search_calls == 0 and citations == 0:
            return _result(ok=False, provider=provider_id, model=model, status=status,
                           latency_ms=latency_ms, search_calls=search_calls,
                           citations=citations, message=_GEMINI_NOT_SEARCHED)
        return _result(ok=True, provider=provider_id, model=model, status=status,
                       latency_ms=latency_ms, search_calls=search_calls,
                       citations=citations,
                       message=f"模型原生搜索可用（google_search {search_calls} 次，"
                               f"引用 {citations} 条）。")
    except Exception as error:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        timeout = isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException))
        return _result(ok=False, provider=provider_id, model=model, status=None,
                       latency_ms=latency_ms, search_calls=0, citations=0,
                       message=_TIMEOUT_MESSAGE if timeout else _NETWORK_MESSAGE)


async def test_native_search(config: Mapping[str, Any], provider_id: str, *,
                             client: httpx.AsyncClient | None = None,
                             transport: httpx.AsyncBaseTransport | None = None,
                             timeout_ms: float = 60000) -> dict[str, Any]:
    """真实测试一个 provider 模型是否会执行原生网页搜索。

    Args:
        config: 档案配置（含 ``provider`` / ``provider_sources``）。
        provider_id: 模型 provider id。
        client: 测试注入的 httpx 客户端。
        transport: 测试注入的 httpx transport（``httpx.MockTransport``）。
        timeout_ms: 请求超时（毫秒，最小 5000）。

    Returns:
        ``{ok,provider,model,status,latencyMs,searchCalls,citations,message}``。
    """
    resolved = provider_source(config, provider_id)
    if resolved is None:
        return _result(ok=False, provider="" if provider_id is None else provider_id,
                       model="", status=None, latency_ms=0, search_calls=0,
                       citations=0, message="找不到该模型 Provider。")
    model = resolved["model"]
    api_base = resolved["api_base"]
    key = resolved["key"]
    if not api_base or not key:
        return _result(ok=False, provider=provider_id, model=model, status=None,
                       latency_ms=0, search_calls=0, citations=0,
                       message="该 Provider 缺少 api_base 或 Key，无法测试。")
    seconds = max(5.0, float(timeout_ms) / 1000.0 if timeout_ms else 60.0)
    resolved_client, owns_client = _client_for(client, transport, seconds)
    try:
        source = resolved["source"]
        if _is_gemini(source, model, api_base):
            gemini = await _run_gemini(client=resolved_client, provider_id=provider_id,
                                       model=str(model), api_base=api_base,
                                       key=str(key), seconds=seconds)
            if gemini["ok"]:
                return gemini
            fallback = await _run_responses(client=resolved_client,
                                            provider_id=provider_id, model=str(model),
                                            api_base=api_base, key=str(key),
                                            seconds=seconds)
            fallback["message"] = (
                f"{fallback['message']}"
                f"（Gemini 原生搜索不可用，已回退 Responses：{gemini['message']}）"
            )
            return fallback
        return await _run_responses(client=resolved_client, provider_id=provider_id,
                                    model=str(model), api_base=api_base,
                                    key=str(key), seconds=seconds)
    finally:
        if owns_client:
            try:
                await resolved_client.aclose()
            except Exception:  # noqa: BLE001
                pass
