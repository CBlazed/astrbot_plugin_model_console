# 架构与接口契约

本文是插件的技术契约：平台事实、HTTP 端点、Python 模块接口、状态目录与测试约定。
行为基准是旧版独立 Node 应用（`model-console`）的语义；响应 JSON 逐字段对等，差异见 [DEVIATIONS.md](DEVIATIONS.md)。

## 1. 运行形态

- **前端**：`pages/model-console/` 是 AstrBot 官方 Plugin Pages，在 Dashboard 的受限 iframe 内运行，
  入口自动出现在 WebUI 左侧「扩展功能」组；页面通过 `window.AstrBotPluginPage` bridge 调后端。
- **后端**：Python，全部路由经 `context.register_web_api()` 注册，由 Dashboard 转发。
- **不代理模型生成、不新增 provider、不编辑凭证**；远程设备默认只读。

## 2. 已确认的平台事实

- AstrBot ≥ 4.24.2 提供 Plugin Pages；本插件最低版本声明即此值。
- 页面 iframe `sandbox="allow-scripts allow-forms allow-downloads"`，**没有 `allow-same-origin`**：
  - 页面内 `localStorage` / `sessionStorage` 会抛 `SecurityError`；
  - 不能直接 `fetch` 后端（无 Cookie），**所有请求必须走 bridge**。
- bridge 能力：`ready / getContext / getLocale / getI18n / t / onContext / apiGet / apiPost / upload / download / subscribeSSE`；
  **没有 PUT/DELETE**；endpoint 是插件内相对路径（不带插件名前缀、不带前导 `/`）。
- bridge 返回值规则：后端返回普通 JSON → resolve 整个 JSON；`{status:"ok",data:x}` → resolve `x`；
  `{status:"error"}` 或非 2xx → reject（只保留 message，**丢失错误码**）。
- **因此本插件约定**：
  - 成功：HTTP 200 + 业务 JSON；
  - 业务错误：**HTTP 200 + `{"error": "<稳定错误码>", ...}`**（前端据此抛带 code 的 Error）；
  - 只有「路由不存在 / 参数类型非法 / 未捕获异常」才用 `error_response(...)`（bridge 会 reject）。
- sandbox 没有 `allow-modals`：`window.confirm/alert` 被静默拒绝，确认交互必须用页面内元素（`<dialog>` 等）。
- 静态资源只重写**相对路径**（`./app.mjs`、`./styles.css`、CSS `url(...)`、JS `import`）；`/` 开头的绝对路径会 404。
- 插件数据目录：`astrbot.core.utils.astrbot_path.get_astrbot_plugin_data_path()` → `<DATA>/plugin_data`。
- Dashboard 端口解析顺序：`DASHBOARD_PORT` → `ASTRBOT_DASHBOARD_PORT` → `cmd_config.json → dashboard.port` → 日志末条 `Starting WebUI at`。
- Dashboard API 鉴权：HS256 JWT，`secret = cmd_config.json → dashboard.jwt_secret`，claim 只含 `{"username": <dashboard.username>}`。
- 运行环境：Python 3.12，可用 httpx / aiohttp / cryptography / sqlite3 / ctypes；**不新增第三方依赖**。
- SSH 走本机 `ssh.exe`（argv 数组、不经 shell）；DPAPI 用 `ctypes` 调 `crypt32`（CurrentUser + `CRYPTPROTECT_UI_FORBIDDEN`），密文前缀 `dpapi:`，失败抛 `SECURE_STORE_UNAVAILABLE`。

## 3. 端点表

注册路由一律 `/<PLUGIN_NAME>/<path>`；bridge endpoint 为去掉 `/<PLUGIN_NAME>/` 的路径。

| 方法 | bridge endpoint | 说明 |
| --- | --- | --- |
| GET | `health` | 运行态：`{status,version,dataPath,stateDir,readOnly,startedAt}` |
| GET | `state` | `{stateDir,legacyStateDir,legacyImported,legacyImport,legacyStatePresent,editMode,writable,astrbotData}` |
| POST | `state/migrate` | 手动重跑旧状态导入 |
| GET | `snapshot?device=` | 快照（资源/绑定/模型/路由/特性探测） |
| GET | `capabilities?provider_id=` | 推理档位 `{levels,verified}` |
| GET | `backups?device=` | 备份列表 + `stats` + `limits` + `warnings` |
| GET | `backups/<backup_id>/diff?kind=&id=` | 恢复前已审查字段差异 |
| POST | `backups/prune` | `{keep?}` → `{removed,stats}` |
| POST | `restore-plans` | `{backupId,resource,expectedRevision?}` → 恢复计划 |
| GET | `devices` | 设备列表（含本机，凭据不回显） |
| POST | `devices` | 添加设备 `{name,kind,baseUrl,apiKey?,ssh?,pairingCode?,readOnly?}` |
| GET | `devices/<device_id>/health` | 连通性 |
| POST | `devices/<device_id>/delete` | 删除设备（替代旧 `DELETE`） |
| POST | `plans` | `{resource,changes,expectedRevision}`；`device` 可放 body |
| POST | `apply` | `{id}`；`device` 可放 body |
| POST | `search-native-test` | `{profileId,providerId?}` |
| POST | `search-key-test` | `{provider,key?,profileId?}` |
| GET | `diagnostics/export` | 导出脱敏诊断 |

请求体上限 64 KiB（`BODY_TOO_LARGE`），POST 一律 `application/json`。

**设备目标传递**：bridge 的 `apiPost(endpoint, body)` 不能携带 query（只有 `apiGet` 支持 params），
所以 `plans` / `apply` 的设备目标 **query `?device=` 优先、`body.device` 兜底**；`main.py` 会把 `device`
从 body 移除后再进业务层。`search-*` 不受 device 影响；诊断导出的数据仍由 GET 获取。

## 4. 模块职责

| 模块 | 职责 |
| --- | --- |
| `errors.py` | `ConsoleError` 承载旧实现 `Error.code` 语义；稳定错误码收敛 |
| `paths.py` | 状态目录解析、可写探针、data 目录发现、旧状态一次性导入与记录 |
| `registry.py` | 字段表（6 核心 + 12 网页搜索设置）、secret 判定、未审查字段发现 |
| `gateway.py` | AstrBot Dashboard API 客户端：单 `httpx.AsyncClient` + 自签 JWT、端口四级解析、缓存 |
| `engine.py` | 快照、计划、应用、恢复计划、revision、审计、secret 掩码 |
| `backup.py` | 备份/清理/统计、SQLite online backup + quick_check、manifest sha256 |
| `diagnostics.py` | 白名单事件、脱敏、1 MiB 轮转、每分钟限速 |
| `app.py` | 编排与参数校验；只读门禁；远程分支调度 |
| `devices.py` / `secure_store.py` | 设备注册表 + DPAPI 凭据保护 |
| `ssh_tunnel.py` / `http_adapter.py` | SSH 隧道与 OpenAPI Key 只读 adapter |
| `agent.py` / `agent_adapter.py` / `remote.py` | Agent 服务端与客户端、远程调度 |
| `search_key_test.py` / `native_search_test.py` | 搜索链路真实测试（含 Gemini 附加分支） |

接口签名以各模块 docstring 与测试为准；`app.py` 只做编排，业务规则留在各模块。

## 5. 状态目录与迁移

- 默认：`<plugin_data>/astrbot_plugin_model_console/state`（`MODEL_CONSOLE_STATE` 或配置项 `state_dir` 可覆盖）。
- 布局与旧版一致：`backups/<uuid>/`、`audit.jsonl`、`diagnostics.jsonl`、`devices.json`、`secrets.json`、`legacy-import.json`。
- 首次启动：若旧目录 `%LOCALAPPDATA%\AstrBotModelConsole\state` 存在，一次性复制到新状态目录（不覆盖、不删源），
  并把结果持久化到 `legacy-import.json`（`imported` 粘性：已导入即 true，重载不回退）。
- `edit_mode=false`：`/health.readOnly=true`，写接口返回 `READ_ONLY_MODE`；GET 正常。

## 6. 代码规范

- Python 3.12：`from __future__ import annotations`、类型注解、模块/公开函数 docstring。
- 全部 I/O 异步或 `asyncio.to_thread`；不在事件循环里同步读大文件/同步 HTTP。
- 复用 `httpx.AsyncClient`（15–30s 超时），`close()` 释放。
- `snapshot()` 内并行 `asyncio.gather` 读取各资源，避免串行等待。
- 日志用 `astrbot.api.logger`；错误信息不得包含密钥、完整配置或 JWT。
- 不写全局可变状态；限速窗口挂在 `ConsoleApp` 实例上。

## 7. 测试

- 标准库 `unittest`，`python -m unittest discover -s tests -t . -v`，不依赖 AstrBot 进程。
- 用假 `Gateway`、临时目录、`httpx.MockTransport`；**不发真实外网请求、不碰真实配置**。
- 覆盖重点：路径解析/迁移、字段表与 secret 判定、revision 冲突、备份失败阻断、只读门禁、
  设备校验与凭据不泄露、SSH 端口解析、DPAPI 降级、搜索 provider 校验与限速。
