"""AstrBot 模型切换器插件入口。

职责：
1. 读取插件配置，创建 :class:`console.app.ConsoleApp`（全部业务逻辑在 ``console/`` 包里）。
2. 用 ``context.register_web_api()`` 注册页面可调用的后端 API；路由与 bridge endpoint
   接口契约与模块划分见 docs/ARCHITECTURE.md。
3. 统一响应约定：成功 = HTTP 200 + 业务 JSON；业务错误 = HTTP 200 + ``{"error": CODE}``
   （bridge 在非 2xx 时只会保留 message，会丢错误码，所以不靠状态码表达业务错误）。

接口契约与模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .console.app import ConsoleApp
from .console.errors import ConsoleError

PLUGIN_NAME = "astrbot_plugin_model_console"
PLUGIN_VERSION = "v0.1.0"
MAX_BODY_BYTES = 64 * 1024

HandlerResult = dict[str, Any]


@register(
    PLUGIN_NAME,
    "CBlazed",
    "把独立 Node 模型控制台重写为插件：在 WebUI 里集中查看与更换 AstrBot 核心、bot 配置与"
    "插件使用的模型，带草稿预览、写前备份、写后回读、备份恢复、远程设备与搜索链路测试。",
    PLUGIN_VERSION,
    "",
)
class ModelConsolePlugin(Star):
    """模型切换器插件。

    只做「配置 + 路由 + 响应收敛」，业务实现全部在 ``console.*`` 模块里。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = dict(config or {})
        plugin_data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.app = ConsoleApp(self.config, plugin_data_dir)
        self._register_routes(context)

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件加载后初始化：解析目录、准备状态目录、按需导入旧控制台状态。"""
        try:
            await self.app.initialize()
        except ConsoleError as exc:
            self.logger.error("[model_console] 初始化失败：%s %s", exc.code, exc.message)
        except Exception:  # noqa: BLE001 - 插件加载不能因未预期异常中断
            self.logger.exception("[model_console] 初始化出现未预期异常")

    async def terminate(self) -> None:
        """插件卸载/重载时释放 HTTP 客户端等资源。"""
        try:
            await self.app.close()
        except Exception:  # noqa: BLE001
            self.logger.exception("[model_console] 关闭资源时出现未预期异常")

    # ------------------------------------------------------------------ 路由注册

    def _register_routes(self, context: Context) -> None:
        """把全部 Page API 注册到 AstrBot Dashboard。"""
        routes: list[tuple[str, Callable[..., Awaitable[HandlerResult]], list[str], str]] = [
            ("health", self._get_health, ["GET"], "模型切换器运行态"),
            ("state", self._get_state, ["GET"], "模型切换器状态目录与迁移状态"),
            ("state/migrate", self._post_state_migrate, ["POST"], "导入旧控制台状态"),
            ("snapshot", self._get_snapshot, ["GET"], "配置快照"),
            ("capabilities", self._get_capabilities, ["GET"], "模型推理档位"),
            ("backups", self._get_backups, ["GET"], "备份列表"),
            ("backups/<backup_id>/diff", self._get_backup_diff, ["GET"], "恢复前差异"),
            ("backups/prune", self._post_backups_prune, ["POST"], "清理旧备份"),
            ("restore-plans", self._post_restore_plans, ["POST"], "生成恢复计划"),
            ("devices", self._get_devices, ["GET"], "远程设备列表"),
            ("devices", self._post_devices, ["POST"], "添加远程设备"),
            ("devices/<device_id>/health", self._get_device_health, ["GET"], "远程设备连通性"),
            ("devices/<device_id>/delete", self._post_device_delete, ["POST"], "删除远程设备"),
            ("plans", self._post_plans, ["POST"], "生成修改计划"),
            ("apply", self._post_apply, ["POST"], "应用计划"),
            ("search-native-test", self._post_search_native_test, ["POST"], "测试模型原生搜索"),
            ("search-key-test", self._post_search_key_test, ["POST"], "测试内置搜索 Key"),
            ("diagnostics/export", self._get_diagnostics_export, ["GET"], "导出脱敏诊断"),
        ]
        for path, handler, methods, desc in routes:
            context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)

    # ------------------------------------------------------------------ 响应收敛

    async def _respond(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """执行一个业务调用并按契约收敛响应。

        Args:
            operation: 无参协程工厂。

        Returns:
            AstrBot 响应对象。
        """
        try:
            return json_response(await operation(), status_code=200)
        except ConsoleError as exc:
            return json_response(exc.to_body(), status_code=200)
        except Exception:  # noqa: BLE001 - 未预期异常不能把栈暴露给页面
            self.logger.exception("[model_console] 未预期异常")
            return error_response("UNEXPECTED", status_code=409)

    @staticmethod
    async def _json_body() -> dict[str, Any]:
        """读取并校验 JSON 请求体（上限 64 KiB）。

        Returns:
            解析后的对象。

        Raises:
            ConsoleError: 非 JSON、超限或不是对象。
        """
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ConsoleError("BODY_TOO_LARGE")
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConsoleError("INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise ConsoleError("INVALID_BODY")
        return payload

    @staticmethod
    def _query_device() -> str | None:
        """读取 ``?device=`` 查询参数（空串按 None 处理）。"""
        value = request.query.get("device")
        return value or None

    # ------------------------------------------------------------------ GET

    async def _get_health(self) -> Any:
        return await self._respond(self.app.health)

    async def _get_state(self) -> Any:
        return await self._respond(self.app.state)

    async def _get_snapshot(self) -> Any:
        device_id = self._query_device()
        return await self._respond(lambda: self.app.snapshot(device_id))

    async def _get_capabilities(self) -> Any:
        provider_id = request.query.get("provider_id")
        return await self._respond(lambda: self.app.capabilities(provider_id))

    async def _get_backups(self) -> Any:
        device_id = self._query_device()
        return await self._respond(lambda: self.app.backups(device_id))

    async def _get_backup_diff(self, backup_id: str) -> Any:
        query = {
            "kind": request.query.get("kind") or "",
            "id": request.query.get("id") or "",
        }
        return await self._respond(lambda: self.app.backup_diff(backup_id, query))

    async def _get_devices(self) -> Any:
        return await self._respond(self.app.devices)

    async def _get_device_health(self, device_id: str) -> Any:
        return await self._respond(lambda: self.app.device_health(device_id))

    async def _get_diagnostics_export(self) -> Any:
        return await self._respond(self.app.diagnostics_export)

    # ------------------------------------------------------------------ POST

    async def _post_state_migrate(self) -> Any:
        return await self._respond(lambda: self._with_body(self.app.migrate_state))

    async def _post_backups_prune(self) -> Any:
        return await self._respond(lambda: self._with_body(self.app.prune_backups))

    async def _post_restore_plans(self) -> Any:
        return await self._respond(lambda: self._with_body(self.app.restore_plan))

    async def _post_devices(self) -> Any:
        return await self._respond(lambda: self._with_body(self.app.add_device))

    async def _post_device_delete(self, device_id: str) -> Any:
        return await self._respond(lambda: self.app.delete_device(device_id))

    async def _post_plans(self) -> Any:
        return await self._respond(lambda: self._with_device_body(self.app.plans))

    async def _post_apply(self) -> Any:
        return await self._respond(lambda: self._with_device_body(self.app.apply))

    async def _post_search_native_test(self) -> Any:
        return await self._respond(lambda: self._with_body(self.app.search_native_test))

    async def _post_search_key_test(self) -> Any:
        return await self._respond(lambda: self._with_body(self.app.search_key_test))

    async def _with_body(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> Any:
        """先解析请求体再调用业务方法。

        Args:
            handler: 接收请求体 dict 的协程函数。

        Returns:
            业务返回值。
        """
        return await handler(await self._json_body())

    async def _with_device_body(
        self,
        handler: Callable[[dict[str, Any], str | None], Awaitable[Any]],
    ) -> Any:
        """解析请求体并解析设备目标，再调用业务方法。

        bridge 的 ``apiPost`` 不能携带 query 参数，所以设备目标同时支持两种来源：
        query ``?device=``（外部调用）优先，``body.device``（页面内调用）兜底。
        ``device`` 字段会从请求体里移除，避免透传给远端 Agent。

        Args:
            handler: 接收 (请求体, 设备 id) 的协程函数。

        Returns:
            业务返回值。
        """
        body = await self._json_body()
        device_id = self._query_device()
        if device_id is None:
            raw_device = body.pop("device", None)
            if isinstance(raw_device, str) and raw_device and raw_device != "local":
                device_id = raw_device
            elif isinstance(raw_device, str):
                device_id = None
        else:
            body.pop("device", None)
        return await handler(body, device_id)
