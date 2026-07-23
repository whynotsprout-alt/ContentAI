# ContentAI V0.5.0 第三轮复审修复设计

## 背景

V0.5.0 RC 第三轮独立复审确认此前修复已覆盖 runtime 引用保留、DNS 并发上限、管理端保存状态、终态取消投影和 Ubuntu 制品确定性主体，但仍有三条 Important 与一条 Minor 需要收口：

- LLM client 的 close Future 在完成与 callback 安装之间存在并发竞态。
- runtime cache owner 在底层异步关闭真正成功前即永久进入 closed，失败后缺少可靠重试入口。
- Ubuntu 打包脚本依赖 Windows PowerShell 5 的 native argv 引号处理，PowerShell 7 会把反斜杠写入 manifest。
- stale cancellation 的回归测试只覆盖 disabled-user 分支，没有覆盖 active-user 的第二个投影点。

本轮只修复这些已确认问题，不扩展到 runtime 全面租约重写或发布流程重构。

## 目标

- 所有并发 close 调用在一次关闭状态转换完成前共享同一个 canonical Future。
- 底层关闭成功前，gateway owner 不得永久进入 closed；失败后必须存在明确、可测试的重试路径。
- FastAPI 异步 lifespan 必须等待 runtime 持有的模型资源完成关闭。
- cache eviction 和 container shutdown 使用同一套 owner 生命周期语义。
- Windows PowerShell 5 与 PowerShell 7 均生成合法、字节确定的 `release-manifest.json`。
- active-user 与 disabled-user 两条 waiting-input cancellation 分支都具备终态接管回归覆盖。

## 非目标

- 不删除 `_RetainedRuntimeValue`，不把所有 runtime 使用方改成显式 context manager。
- 不改变模型调用、缓存键、缓存容量或 eviction 顺序。
- 不改变 Ubuntu 归档内容、目录前缀、checksum 格式或发布版本命名。
- 不改变 cancellation 的生产行为；Minor finding 只补充缺失测试。
- 不在本轮运行无明确终态预算的完整后端测试集。

## 关闭生命周期

### Client 层

`_OwnerLoopAsyncClient` 继续负责在专属 event loop 上关闭 `httpx.AsyncClient`。`_submit_close()` 在 callback 完成状态转换前始终复用当前 Future，包括 Future 已完成但 callback 尚未安装或尚未执行的窗口。

每次真正提交 close task 时递增 generation。done callback 只有在 Future identity 与 generation 都仍为当前 canonical close 时才能修改状态：

- 成功：标记 async client 已关闭，并停止 owner loop。
- 失败或取消：清除 canonical Future，使下一次调用可以提交唯一 retry。
- 过期 callback：不清理 successor Future，也不停止 owner loop。

同步 `close()` 保持现有兼容语义；异步 `aclose()` 等待 canonical Future 并传播底层关闭错误。

### Gateway owner 层

`_GatewayOwner` 使用 `active`、`closing`、`closed` 三态，而不是在调用 `gateway.close()` 前设置布尔 closed。

- `active`：仍可 retain，或已 retired 但仍有引用。
- `closing`：满足 retired 且引用数为零，已启动一次 canonical close。
- `closed`：底层 gateway 已确认关闭成功。

关闭失败时 owner 从 `closing` 回到可重试状态，并保留错误供显式 shutdown 传播。并发 release、retire、同步 close 和异步 close 不能提交多个底层关闭任务。`release()` 作为 finalizer 入口只记录失败而不向 finalizer 抛出；显式 `close()` / `aclose()` 才负责向调用方传播关闭错误。

`weakref.finalize` 仍用于释放 runtime wrapper 的引用计数，但不再承担唯一的最终资源关闭责任。finalizer 只触发 owner 状态推进；真正的应用 shutdown 由 container 显式等待。

### Container 与 lifespan

`RuntimeContainer` 保存尚未成功关闭的 retired owner，作为 cache entry 被移除后的重试登记表。`RuntimeContainer.aclose()` 负责：

1. 在 cache lock 下摘取当前所有 entry，并将其标记 retired。
2. 清空 entry 中的 gateway、model 与 graph wrapper，使无外部引用的 wrapper 可释放 owner 引用。
3. 收集所有需要关闭或正在关闭的 owner。
4. 在 lock 外等待每个 owner 完成关闭，并传播无法恢复的关闭错误。
5. 最后关闭 container 自己拥有的 checkpointer。

`RuntimeContainer.close()` 保留给同步调用方：无 running loop 时等待关闭并传播错误；running loop 内只能启动 canonical close，调用方必须改用 `aclose()` 才能获得完成保证。该兼容接口不作为 FastAPI shutdown 的完成门禁。

`AgentService.aclose()` 先执行 runner 的同步收尾，再等待 `RuntimeContainer.aclose()`。FastAPI 的 `_shutdown()` 改为异步函数：同步关闭 conversation service，等待 agent service 的 `aclose()`，最后关闭数据库。每个 hook 独立记录失败，前一个 hook 失败不阻止后续资源收口。lifespan 在 `finally` 中 `await _shutdown(app)`，确保 running-loop 路径不会只提交关闭任务就返回。

被容量淘汰的 entry 需要保留一个轻量 retired owner 句柄，直到关闭成功。失败 owner 会在后续显式 `aclose()` 中重试，避免 daemon owner loop 和 transport 永久泄漏。

## Ubuntu Manifest 注入

脚本不再把 JSON 内容跨 PowerShell native argv 边界传给 Git。它在唯一临时目录中创建叶名固定为 `release-manifest.json` 的文件，使用 `[IO.File]::WriteAllText` 与 `Text.UTF8Encoding($false)` 写入无 BOM 的压缩 JSON，再通过 `git archive --add-file=<temporary-manifest-path>` 注入归档；现有 `--prefix` 继续把该叶名放到发布目录根下。

临时 manifest 的创建、归档调用和清理由 `try/finally` 包围。实现不得引入 PowerShell 版本分支、shell quote escaping 或 `--add-virtual-file`；成功与失败路径都必须清理临时文件和目录。

测试不再在 `powershell` 与 `pwsh` 之间二选一，而是枚举当前环境实际可用的两个宿主并分别执行两次隔离构建。静态契约要求脚本使用 `--add-file` 和无 BOM 写入 API，并拒绝 `$escapedManifest` 与 `--add-virtual-file`。

两次隔离构建必须继续满足：完整 archive bytes 相等、checksum 文本相等、manifest 不含 UTF-8 BOM、JSON 合法且字段符合发布契约。

## Cancellation 测试补齐

新增 active user 参数化案例，覆盖 competing transaction 将 execution 与 attempt 更新为 `completed` 或 `failed`，同时保留 stale `cancel_requested_at` 的场景。

测试必须断言：

- execution 和 attempt 保持 competing terminal 状态与错误信息。
- 不发送 `run_cancel`。
- 不发送 `attempt_end(status=cancelled)`。
- active user 状态不被测试夹具改写，从而确保命中 runner 的第二个 cancellation consumer。

生产代码不因该 Minor finding 发生变化。

## 错误处理

- client close 失败通过 Future/await 原样传播，下一次 close 可重试。
- owner close 失败不得伪装为 closed；显式 container shutdown 必须看到失败。
- finalizer 路径不能抛出 unraisable exception；失败状态留给显式 shutdown 重试和报告。
- 若某个 owner 最终关闭失败，container 仍应尝试关闭其余 owner，再汇总或传播首个错误，避免单点失败阻断其他资源释放。
- 打包脚本在 manifest 无法解析或归档命令失败时不得生成成功制品结论。

## 测试策略

实施遵循 TDD，并按 finding 分组验证：

1. 保留已完成的 immediate-completion barrier 并发测试，验证 canonical Future、失败后唯一 retry 和 owner loop 停止。
2. 为 `_GatewayOwner` 增加 flaky gateway 测试，先证明当前实现会提前 closed 或失去 retry，再实现三态生命周期。
3. 增加 container eviction 与 async shutdown 集成测试，验证首次关闭失败后显式 `aclose()` 能重试并最终停止底层 owner loop。
4. 先增加 `--add-file`、无 BOM 写入和禁止 shell escaping 的静态失败契约，再用临时 manifest 文件实现注入；在每个可用 PowerShell 宿主上执行真实双构建。
5. 增加 active-user terminal takeover cancellation 参数化测试，确认测试在未改生产逻辑时直接通过。

聚焦验证包括相关 Pytest 文件、受影响 Python 文件的 Ruff、`compileall`、PowerShell AST 解析、Ubuntu 双归档字节比较以及 `git diff --check`。若完整后端测试无法在既定时间内取得终态，只记录实际证据，不宣称全量通过。

## 交付边界

本轮修改预计限制在：

- `apps/api/src/agent/infrastructure/llm/client.py`
- `apps/api/src/agent/infrastructure/llm/gateway.py`
- `apps/api/src/agent/runtime/container.py`
- `apps/api/src/services/agent_service.py`
- `apps/api/src/api/app.py`
- `apps/api/tests/test_llm_client.py`
- `apps/api/tests/test_runtime_container.py`
- `apps/api/tests/test_api.py`
- `tools/package-ubuntu.ps1`
- `apps/api/tests/test_release_contract.py`

实现完成后更新本地复审证据，并对第三轮 findings 逐条核销。未经用户明确授权，不创建提交、不推送分支、不创建 PR。
