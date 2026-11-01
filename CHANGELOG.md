# 更新日志

## v0.1.1（2026-11-01）

上架审核修复：移除内置日志模块的回退分支。

- **合规**：`console/app.py` 的日志记录器改为无条件从 `astrbot.api` 导入，
  删除 `try/except` 回退与内置日志模块的导入 / `getLogger` 调用
  （插件商店上架规范：日志记录器必须且只能从 `astrbot.api` 导入）；
- **测试**：新增 `tests/_astrbot_stub.py`，在未安装 AstrBot 的单测环境注入最小
  `astrbot.api.logger` 桩（不使用内置日志模块），由 `tests/__init__.py` 安装；
- **防回归**：新增 `tests/test_compliance.py`，静态检查全仓禁止内置日志模块导入、
  `getLogger` / `basicConfig` / `eval` / `exec` / `shell=True` / `requests` / `pickle.loads`，
  并断言 `astrbot.api.logger` 的导入位于模块顶层、不被 `try/except` 包裹；
- 版本号 `v0.1.0 → v0.1.1`（`metadata.yaml`、`main.py`、`console/app.py` 同步）。

## v0.1.0（2026-10-31）

首个版本：把独立 Node 版模型控制台（model-console）重写为 AstrBot 插件。

### 功能
- 模型绑定总览：每个配置档案 6 类核心绑定（主对话/压缩/图片描述/STT/TTS/长期记忆图片描述）、
  15 个插件模型字段、模型库与推理强度、网页搜索设置与 9 个内置搜索 API Key；
- 安全编辑：草稿 → 预览 → 逐资源应用，revision 双校验、写前备份、写后回读、审计留痕、冲突 diff；
- 备份/恢复/诊断：备份列表与统计、保留策略、恢复前 diff、恢复计划、脱敏诊断导出；
- 远程设备：HTTP（OpenAPI Key，只读）、SSH（隧道 + 端口自动发现，只读）、Agent（配对码，可 `--enable-writes`）；
- 搜索链路测试：模型原生搜索（OpenAI Responses；Gemini 走原生 grounding 并回落）、内置搜索 Key 测试。

### 平台
- 通过 AstrBot 官方 Plugin Pages 提供页面，入口自动出现在 WebUI 左侧「扩展功能」组；
- 默认编辑模式可写（`edit_mode`），可关闭为只读；
- 状态目录迁到插件数据目录，首次启动自动导入旧 `%LOCALAPPDATA%\AstrBotModelConsole\state`；
- 零第三方依赖，不再需要 Node.js。

### 已知差异
见 [docs/DEVIATIONS.md](docs/DEVIATIONS.md)。
