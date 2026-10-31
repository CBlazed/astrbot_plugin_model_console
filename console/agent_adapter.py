"""Hub 侧远程 Model Console agent 客户端（``/agent/v1/*``）。

对齐旧 Node 实现 ``model-console/src/agent-adapter.mjs``：

* 8 位一次性配对码换 64 hex 设备 token；token 只放 ``Authorization: Bearer``；
* 错误码稳定：``AGENT_UNAUTHORIZED`` / ``AGENT_UNREACHABLE`` / ``AGENT_INVALID_RESPONSE`` /
  ``AGENT_RATE_LIMITED`` / ``AGENT_READ_ONLY``；
* 写侧 409 会把 agent 的业务错误码与白名单 ``details`` 透传上来（不泄露路径）；
* ``pair_agent`` 是设备层的一次性配对便捷入口。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import httpx

from .errors import ConsoleError

AGENT_UNAUTHORIZED = "AGENT_UNAUTHORIZED"
AGENT_UNREACHABLE = "AGENT_UNREACHABLE"
AGENT_INVALID_RESPONSE = "AGENT_INVALID_RESPONSE"
AGENT_RATE_LIMITED = "AGENT_RATE_LIMITED"
AGENT_READ_ONLY = "AGENT_READ_ONLY"
AGENT_TIMEOUT_MS = 10000

TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$", re.IGNORECASE)
BASE_URL_PATTERN = re.compile(r"^https?://[^\s/]+(?:/[^\s]*)?$", re.IGNORECASE)
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _coded_error(code: str, *, status: int | None = None,
                 details: dict[str, Any] | None = None) -> ConsoleError:
    """构造带稳定错误码的异常（message 与 code 相同，与旧实现一致）。"""
    return ConsoleError(code, code, details, status if status is not None else 400)


class AgentAdapter:
    """单个远程 agent 的最小客户端（默认只读；写能力由 agent 端开关决定）。"""

    def __init__(self, *, base_url: str, token: str = "",
                 client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout_ms: float = AGENT_TIMEOUT_MS) -> None:
        """创建客户端；``client`` / ``transport`` 是测试注入点。

        Raises:
            ConsoleError: ``AGENT_INVALID_BASE_URL``。
        """
        trimmed = base_url.strip() if isinstance(base_url, str) else ""
        if not trimmed or not BASE_URL_PATTERN.match(trimmed):
            raise _coded_error("AGENT_INVALID_BASE_URL")
        self.base_url = trimmed.rstrip("/")
        self.token = (token.lower()
                      if isinstance(token, str) and TOKEN_PATTERN.match(token) else None)
        self.timeout_ms = (float(timeout_ms)
                           if isinstance(timeout_ms, (int, float)) and timeout_ms > 0
                           else float(AGENT_TIMEOUT_MS))
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=self.timeout_ms / 1000.0,
                transport=transport,
                follow_redirects=False,
            )
        self._client = client

    async def _request(self, method: str, path: str, *,
                       body: Mapping[str, Any] | None = None,
                       requires_token: bool = False,
                       relay_errors: bool = False) -> dict[str, Any]:
        """发一次 agent 请求并按旧实现映射错误码。"""
        if requires_token and not self.token:
            raise _coded_error(AGENT_UNAUTHORIZED)
        headers = {"Accept": "application/json"}
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["content"] = json.dumps(body).encode("utf-8")
        if requires_token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = await self._client.request(
                method, f"{self.base_url}{path}", **kwargs)
        except Exception:  # noqa: BLE001 - 网络错误/超时统一不可达
            raise _coded_error(AGENT_UNREACHABLE) from None
        if response.is_redirect:
            raise _coded_error(AGENT_UNREACHABLE)
        status = response.status_code
        if 200 <= status < 300:
            try:
                parsed = response.json()
            except ValueError:
                raise _coded_error(AGENT_INVALID_RESPONSE) from None
            if not isinstance(parsed, dict):
                raise _coded_error(AGENT_INVALID_RESPONSE)
            return parsed
        if status == 401:
            raise _coded_error(AGENT_UNAUTHORIZED)
        if status == 429:
            raise _coded_error(AGENT_RATE_LIMITED)
        if relay_errors:
            payload = self._safe_json(response)
            if status == 403 and payload.get("error") == AGENT_READ_ONLY:
                raise _coded_error(AGENT_READ_ONLY)
            if status == 409:
                raw = payload.get("error")
                code = raw if isinstance(raw, str) and _SAFE_CODE.match(raw) else AGENT_INVALID_RESPONSE
                details = payload.get("details")
                raise _coded_error(
                    code, status=409,
                    details=details if isinstance(details, dict) else None)
        raise _coded_error(AGENT_INVALID_RESPONSE)

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        """尽力解析错误响应体；失败返回空表。"""
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    async def pair(self, code: str) -> str:
        """用一次性配对码换设备 token，并记住它供后续调用使用。"""
        if not isinstance(code, str) or not code or len(code) > 64:
            raise _coded_error(AGENT_INVALID_RESPONSE)
        body = await self._request("POST", "/agent/v1/pair", body={"code": code})
        token = body.get("token")
        if not isinstance(token, str) or not TOKEN_PATTERN.match(token):
            raise _coded_error(AGENT_INVALID_RESPONSE)
        self.token = token.lower()
        return self.token

    async def snapshot(self) -> dict[str, Any]:
        """读取远端 Engine 快照（必须已配对）。"""
        return await self._request("GET", "/agent/v1/snapshot", requires_token=True)

    async def plan(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """在可写 agent 上生成计划（只读 agent 返回 ``AGENT_READ_ONLY``）。"""
        payload: dict[str, Any] = {
            "resource": (body or {}).get("resource"),
            "changes": (body or {}).get("changes"),
        }
        if "expectedRevision" in (body or {}):
            payload["expectedRevision"] = body["expectedRevision"]
        return await self._request("POST", "/agent/v1/plans", body=payload,
                                   requires_token=True, relay_errors=True)

    async def apply(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """按 id 应用计划；返回 ``{result, hotLoad}``。"""
        identity = body.get("id") if isinstance(body, Mapping) else body
        if not isinstance(identity, str) or not identity:
            raise _coded_error(AGENT_INVALID_RESPONSE)
        payload = await self._request("POST", "/agent/v1/apply",
                                      body={"id": identity},
                                      requires_token=True, relay_errors=True)
        if not isinstance(payload.get("result"), dict) or not isinstance(payload.get("hotLoad"), dict):
            raise _coded_error(AGENT_INVALID_RESPONSE)
        return payload

    async def backups(self) -> dict[str, Any]:
        """列出可写 agent 的备份：``{backups,stats,limits}``。"""
        payload = await self._request("GET", "/agent/v1/backups",
                                      requires_token=True, relay_errors=True)
        if (not isinstance(payload.get("backups"), list)
                or not isinstance(payload.get("stats"), dict)
                or not isinstance(payload.get("limits"), dict)):
            raise _coded_error(AGENT_INVALID_RESPONSE)
        return payload

    async def health(self) -> dict[str, Any]:
        """公开健康探测：``{status:'ok',version}``。"""
        payload = await self._request("GET", "/agent/v1/health")
        if payload.get("status") != "ok":
            raise _coded_error(AGENT_INVALID_RESPONSE)
        return payload

    async def close(self) -> None:
        """释放自建 HTTP 客户端；注入的客户端由调用方负责。"""
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass


async def pair_agent(device: Mapping[str, Any], pairing_code: str, *,
                     client: httpx.AsyncClient | None = None,
                     transport: httpx.AsyncBaseTransport | None = None) -> str:
    """一次性配对：``POST /agent/v1/pair`` 并返回设备 token 字符串。"""
    adapter = AgentAdapter(base_url=str((device or {}).get("baseUrl") or ""),
                           client=client, transport=transport)
    try:
        return await adapter.pair(pairing_code)
    finally:
        await adapter.close()
