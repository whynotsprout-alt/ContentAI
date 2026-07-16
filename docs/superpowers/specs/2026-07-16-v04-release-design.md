# ContentAI V0.4 发布设计

## 目标

将已完成的 ContentAI 生产化整理作为正式的 V0.4 发布推送到 GitHub，并提供可部署到 Ubuntu 的源码归档。

## 版本策略

- Git 标签与 GitHub Release 使用 `V0.4`，与现有 `V0.1`、`V0.2`、`V0.3` 保持一致。
- Python 包版本和 Ubuntu 归档使用语义化版本 `0.4.0`。
- Ubuntu 归档命名为 `contentai-0.4.0-ubuntu.tar.gz`。

## 发布范围

- 纳入：已验证的生产化改动，包括精简后的 Agent/会话模型、单一初始迁移、35 个 API 契约、九服务 Compose、容器日志、Ubuntu 运维脚本及同步文档。
- 排除：未跟踪的 `AGENTS.md`。该文件是本地协作规范，不进入 V0.4 发布提交。

## 发布流程

1. 运行全量后端、前端、静态检查及构建验证。
2. 将 `pyproject.toml` 和 Ubuntu 打包脚本中的版本更新为 `0.4.0`，并重新生成归档和 SHA256。
3. 再次运行与版本和归档有关的验证。
4. 仅暂存经确认的生产化文件，排除 `AGENTS.md`，创建 `release: V0.4` 提交。
5. 在 `main` 创建 `V0.4` 标签，推送 `main` 和标签到 `origin`。
6. 创建正式 GitHub Release `ContentAI V0.4`，上传 Ubuntu 归档；发布说明包含主要变更、全新安装限制、验证结果与 SHA256。

## 失败处理

- 任何验证失败时，停止提交和发布，先修复并重新验证。
- 推送或 Release 创建失败时，不重写历史、不强推；保留本地提交和标签并报告准确状态。
- 若归档生成失败，不创建 Release，防止发布无法部署的版本。

## 验收标准

- `AGENTS.md` 未被暂存或提交。
- `pyproject.toml` 的包版本为 `0.4.0`。
- 本地存在并通过哈希记录校验的 `dist/contentai-0.4.0-ubuntu.tar.gz`。
- `main` 已推送，远端存在 `V0.4` 标签和正式 GitHub Release。
- Release 附带 Ubuntu 归档、变更说明及 SHA256。
