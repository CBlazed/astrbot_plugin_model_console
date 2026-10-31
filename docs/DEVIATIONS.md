# 相对旧 Node 基线的已知差异（已逐项确认）

重写目标是「保留所有前后端功能、行为逐字段对等」。以下差异均已确认接受，**没有静默改动**；
编号后的「依据」说明为什么这样处理。

## 一、平台迁移导致的功能替代（不是缺失）

1. **单实例锁 `instance.lock` 移除**：单实例由 AstrBot 插件机制保证。
2. **自带随机端口 / 只监听 127.0.0.1 移除**：页面由 Dashboard 托管，无独立监听端口。
3. **自带会话 token / Host/Origin 校验 / 自带 CSP 移除**：改由 Dashboard JWT 鉴权；
   iframe `sandbox="allow-scripts allow-forms allow-downloads"` 本身隔离 Cookie/Storage/DOM。
4. **`start-console-edit.cmd` 只读/可写开关移除**：改插件配置 `edit_mode`（默认 `true`）。
5. **便携包与 `build-package.mjs` 移除**：插件由 AstrBot 安装/更新。
6. **前端 storage 全部移除**：沙箱 iframe 无 `allow-same-origin`，`localStorage/sessionStorage` 会抛
   `SecurityError`；草稿/设备选择/向导标记改内存态，刷新即丢（页面已提示）。
7. **原生 `confirm/alert` 改为页面内 `<dialog>`**：sandbox 无 `allow-modals`，原生弹窗被静默拒绝。
8. **`DELETE /devices/:id` 改为 `POST devices/<id>/delete`**：bridge 只提供 `apiGet/apiPost`。
9. **`POST plans/apply` 的设备目标**：bridge `apiPost` 不能带 query，改为 query 优先、`body.device` 兜底；
   `main.py` 会把 `device` 从 body 移除后再进业务层。
10. **业务错误一律 HTTP 200 + `{"error": CODE}`**：bridge 遇到非 2xx 只保留 message、丢失错误码。

## 二、实现细节差异（语义等价或更宽容）

11. **`tests/__init__.py` 新增**：`unittest discover -s tests -t .` 要求 tests 是可导入包。
12. **`backup_max_mb = 0` 表示不限**：旧实现无此配置项；默认 2048 MB 时行为一致。
13. **`/backups/prune` 同时执行份数与总大小上限**：契约签名要求 `prune_backups(state_dir, keep, max_bytes)`；
    旧 Node 的 prune 路径只按份数清理。
14. **`expectedRevision: null` 按「未提供」处理**：旧实现会拿 null 参与比对 → `CONCURRENT_CHANGE`。
    前端不会发 null；这里选择更宽容语义。
15. **Gateway 遇 3xx 归为 `ASTRBOT_UNREACHABLE`**：旧 Node `fetch(redirect:'error')` 抛错后被收敛为
    `ASTRBOT_UNREACHABLE`，结果等价。
16. **备份文件系统异常收敛为 `OPERATION_FAILED`**：旧 Node 抛 ENOENT 后由 server 的 `knownError`
    回落为 `OPERATION_FAILED`，响应码一致。
17. **远程层无状态、不缓存**：旧 Node 有 5s 远程快照/会话缓存；新版每次调用自建/关闭 adapter 与 SSH 隧道
    （JSON 与行为一致，SSH 高频刷新会重复建隧道，属性能差异）。
18. **Agent `/agent/v1/health` 的 version 返回插件版本 `v0.1.0`**：旧版读 `package.json` 为 `0.2.2`；
    Hub 只校验 `status=ok`，不依赖版本字段。

## 三、相对基线的扩展（保留，但标注）

19. **`native_search_test` 的 Gemini 分支**：旧实现只有 OpenAI Responses `web_search`；
    新版在检测到 Gemini provider 时额外走 `/v1beta generateContent + google_search`，
    失败自动回落 Responses 并在 message 追加回落原因。**非 Gemini 路径与 8 个响应字段与基线逐字一致。**
20. **新增只读端点 `state` 与 `state/migrate`**：用于页面展示状态目录/迁移结果、手动重跑导入。
21. **修复基线 bug**：旧 `public/app.mjs` 的 `renderDeviceTable` 中 `tr` 未声明（设备表必然 ReferenceError）；
    插件内已补 `const tr = el('tr')`，旧项目只读基线未改。
22. **诊断导出**：页面先 GET 一次取事件数量做提示，再用 bridge `download()` 触发下载（只读、无副作用）。

## 四、需要留意的边界

- **bridge 的 `{status:"ok",data}` 解包规则**：宿主会 `resolve(body.data ?? body)`。因此任何插件响应
  **不要**在顶层同时使用 `status:"ok"` 与 `data` 字段（当前实现没有这种响应）。
- **`hotLoadVerified`**：只表示日志出现重载标记，不代表模型生成成功。
- **备份范围**：插件自己的数据库（如 self_learning `messages.db`）、图片、向量索引不在备份范围内。
- **跨大版本升级**：插件依赖 AstrBot 内部接口（`register_web_api`、dashboard API、`plugin_data` 路径），
  AstrBot 大版本升级后需要回归。

## 五、新旧快照差分结论（2026-10-31 live 实测，已裁示）

23. **binding 内不再重复携带 `identity`**：旧实现 `bindingRows` 用 `{...resource, ...field}`，把 resource 的
    `identity`（含实例/账号/路由状态）复制进**每一条** binding（3 profile × 18 = 54 处重复）。
    新实现只在 `resources[].resource.identity` 保留一份，binding 内不再重复。
    - 影响：页面本来只读 `resource.identity`，UI 无差异；payload 体积显著变小（性能优先）。
    - 结论：**保留差异，不回填**（回填属于冗余数据放大，与「注意性能开销」冲突）。
24. **`revision` 哈希值新旧不同**：旧实现 `sha256(JSON.stringify(canonical(config)))`，新实现用自己的
    规范化（稳定排序 + 紧凑分隔符）计算，同一份配置会得到不同十六进制串。
    - 影响：revision 是**实现内**并发校验用的不透明值，单实现内自洽；旧工具的 expectedRevision 不能跨工具复用。
    - 结论：**保留差异，不回填**（跨工具复用不在目标内）。
25. **`health.version` 与运行态字段**：`0.2.2 → v0.1.0`（产品版本串）；`readOnly/writeEnabled` 取决于
    `edit_mode` 与旧服务启动参数，属运行态配置差异，不算缺陷。
