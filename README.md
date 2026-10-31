# 模型切换器（astrbot_plugin_model_console）

[![tests](https://github.com/CBlazed/astrbot_plugin_model_console/actions/workflows/tests.yml/badge.svg)](https://github.com/CBlazed/astrbot_plugin_model_console/actions/workflows/tests.yml) [![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)


把原先独立的 Node 版「AstrBot 模型控制台（model-console）」**全量重写为 AstrBot 插件**：
在 AstrBot WebUI 里集中查看 / 更换 AstrBot 核心、各 bot 配置档案与插件使用的模型。

- 入口：WebUI 左侧「扩展功能」组 → **模型切换器**（官方 Plugin Pages，页面在受控 iframe 内运行）
- 也可从 扩展功能 → 插件 → 模型切换器 → 页面 打开
- 默认**编辑模式可写**；写入走「草稿 → 预览 → 应用」，带 revision 双校验、写前备份、写后回读、审计留痕
- 状态目录默认在 `AstrBot data/plugin_data/astrbot_plugin_model_console/state`

## 安装

1. 把整个插件目录放到 `<AstrBot data>/plugins/astrbot_plugin_model_console/`；
2. WebUI → 插件 → 重载（或重开 AstrBot）；
3. 侧边栏「扩展功能」出现「模型切换器」，点击进入。

依赖：AstrBot ≥ 4.24.2（Plugin Pages 能力起）；插件本身**不需要** Python 第三方依赖
（只用 AstrBot 运行环境自带的 httpx / sqlite3 / ctypes），也**不再需要 Node.js**。

## 功能

### 模型绑定总览
- 每个配置档案的 6 类核心绑定：主对话、压缩、图片描述、STT、TTS、长期记忆图片描述；
- 插件侧 15 个字段（group_chat_plus / livingmemory / self_learning / meme_manager）；
- 模型库：共享 provider 定义、已登记引用、推理强度（reasoning_effort）；
- 网页搜索设置：开关 + 提供商下拉（检测到并启用 `astrbot_plugin_model_websearch` 时多出「跟随当前聊天模型」）+ 9 个内置搜索 API Key 编辑行（已保存值不回显，只显示「已配置 N 个」）；
- 模型原生搜索测试 / 内置搜索 Key 测试（真实请求，带每分钟限速）。

### 安全编辑
- 草稿只存在页面内存，刷新即丢；不点「应用」不写盘；
- 计划创建、应用前、备份后各核对一次 revision，外部改动返回 `CONCURRENT_CHANGE` 并显示冲突 diff；
- 写前备份（`cmd_config.json` + `data/config/*.json` + `data_v4.db`，逐项 sha256 + `PRAGMA quick_check`），任一步失败**直接阻断写入**；
- 写后回读比较，不一致返回 `VERIFY_FAILED`；
- `audit.jsonl` 记录 `apply-start / applied / apply-failed`（不含完整配置与凭证）。

### 备份、恢复与诊断
- 备份列表、占用统计、清理（保留份数 + 总大小上限，默认 10 份 / 2048 MB）；
- 恢复是**整对象回写**：先看已审查字段 diff → 生成恢复计划 → 预览 → 应用（同样走备份 + revision 校验）；
- 诊断日志 1 MiB 轮转 ×3 份、每分钟 120 条限速、路径与凭据脱敏后可导出。

### 远程设备
| 类型 | 接入方式 | 读写 |
| --- | --- | --- |
| HTTP | AstrBot OpenAPI Key（`abk_`） | 只读 |
| SSH | 本机 `ssh -N -L` 隧道 + 远端日志自动发现 dashboard 端口 | 只读 |
| Agent | 目标机运行 `python -m console.agent`，8 位一次性配对码 | 默认只读；`--enable-writes` 后可远程计划/应用/备份 |

凭据用 Windows DPAPI 加密（`dpapi:` 前缀，CurrentUser）存在插件状态目录；DPAPI 不可用时降级明文并在诊断里留痕。**响应永不回显凭据。**

## 与旧版（独立 Node 应用）的差异

旧版是独立进程，新版本是插件，以下能力**由 AstrBot 接管或不再需要**：

| 旧版 | 新版 |
| --- | --- |
| 自带随机端口 + 只监听 127.0.0.1 | 由 AstrBot Dashboard 托管，页面走 Plugin Pages |
| 会话 token + Host/Origin 校验 + 自己的 CSP | Dashboard JWT 鉴权；页面无 Cookie/DOM 访问能力（iframe sandbox） |
| `instance.lock` 单实例锁 | 单实例由 AstrBot 插件机制保证 |
| `start-console-edit.cmd` 区分只读/可写 | 插件配置 `edit_mode`（默认 `true`） |
| 便携包（内置 node.exe）/ `build-package.mjs` | 不再需要；插件可打包为 zip 由 AstrBot 安装 |
| 状态目录 `%LOCALAPPDATA%\AstrBotModelConsole\state` | `data/plugin_data/astrbot_plugin_model_console/state`，首次启动**自动一次性导入**旧目录（复制、不覆盖、不删源） |

其余行为（接口语义、字段白名单、错误码、备份/审计结构）按旧实现逐字移植，见 [docs/DEVIATIONS.md](docs/DEVIATIONS.md)。

## 插件配置（`_conf_schema.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `edit_mode` | `true` | 允许写入；关闭后所有写接口返回 `READ_ONLY_MODE` |
| `state_dir` | 空 | 覆盖状态目录（留空 = 插件数据目录） |
| `astrbot_data` | 空 | 覆盖 AstrBot data 目录（多实例/远程调试用） |
| `backup_keep` | `10` | 备份保留份数 |
| `backup_max_mb` | `2048` | 备份总大小上限（MB，0 = 不限） |
| `devices_enabled` | `true` | 是否允许添加远程设备 |
| `search_test_enabled` | `true` | 是否允许搜索链路测试 |
| `legacy_state_import` | `true` | 首次启动导入旧控制台状态 |

## 后端 API（页面通过 bridge 调用）

注册路由前缀 `/<plugin_name>/`，bridge endpoint 去掉前缀。全部返回 HTTP 200 + 业务 JSON；
业务错误为 `{"error": "<CODE>", ...}`（bridge 在非 2xx 时会丢错误码，故不靠状态码表达业务错误）。

| 方法 | endpoint | 说明 |
| --- | --- | --- |
| GET | `health` | 运行态（版本/目录/readOnly） |
| GET | `state` | 状态目录、旧状态迁移状态、editMode |
| POST | `state/migrate` | 手动重跑旧状态导入 |
| GET | `snapshot?device=` | 快照（资源/绑定/模型/路由/特性探测） |
| GET | `capabilities?provider_id=` | 推理档位 |
| GET | `backups?device=` | 备份列表 + 统计 + 上限 |
| GET | `backups/<id>/diff?kind=&id=` | 恢复前差异 |
| POST | `backups/prune` | 清理（`{keep?}`） |
| POST | `restore-plans` | 生成恢复计划 |
| GET | `devices` | 设备列表 |
| POST | `devices` | 添加设备 |
| GET | `devices/<id>/health` | 连通性 |
| POST | `devices/<id>/delete` | 删除设备（替代旧 `DELETE`，bridge 不支持 DELETE） |
| POST | `plans` | 生成计划（`device` 可放 body，bridge POST 不能带 query） |
| POST | `apply` | 应用计划 |
| POST | `search-native-test` | 模型原生搜索测试 |
| POST | `search-key-test` | 内置搜索 Key 测试 |
| GET | `diagnostics/export` | 导出脱敏诊断 |

## 开发与测试

```bash
# 开发依赖（插件运行时不需要，只用于跑单测）
python -m pip install -r requirements-dev.txt

# 全部单测（标准库 unittest，不依赖 AstrBot 进程）
python -m unittest discover -s tests -t . -v

# 语法检查
python -m compileall -q main.py console tests
```

设计与接口契约见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，与旧版的差异见 [docs/DEVIATIONS.md](docs/DEVIATIONS.md)。

## 安全边界

- 只操作本机 AstrBot data 目录与插件状态目录；不代理模型生成、不新增 provider、不编辑凭证；
- 远程 HTTP/SSH 设备只读；远程写入只由目标机上的 Agent 执行，并在目标机本地备份与审计；
- `hotLoadVerified: true` 只表示日志出现重载标记，**不代表模型生成成功**；
- 备份/审计/诊断/设备密钥都在状态目录里，**不能分享或打包**；
- 无自动回滚：恢复前先看 diff，恢复是整对象回写。

## 许可

MIT。原项目：`model-console`（同一作者）；重写保留原实现的行为语义与字段契约。
