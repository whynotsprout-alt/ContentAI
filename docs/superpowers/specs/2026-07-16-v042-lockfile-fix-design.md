# ContentAI V0.4.2 锁文件修复设计

## 目标

修复 V0.4.1 的 Docker 镜像无法在 `uv export --locked` 阶段构建的问题，并发布新的不可变 V0.4.2 部署包后启动隔离测试环境。

## 根因

项目版本已递增至 `0.4.1`，但 `uv.lock` 中 editable `contentai` 包仍为 `0.3.0`。Dockerfile 强制使用 `uv export --locked`，因此拒绝使用过期锁文件。

## 决策

- 更新包版本与 Ubuntu 包版本为 `0.4.2`，Git 标签/Release 使用 `V0.4.2`。
- 在同一提交内运行 `uv lock`，使锁文件与 `pyproject.toml` 原子同步。
- 将 `uv lock --check` 和 `docker compose ... build` 加入发布验收。
- 保持 V0.4、V0.4.1 标签、Release 与附件不变。
- 使用新建、独立 Compose 项目和端口启动九服务环境，不触碰用户现有旧数据库容器。

## 验收

- `uv lock --check` 通过，锁文件中的 `contentai` 版本为 `0.4.2`。
- Docker Compose 镜像构建成功。
- 后端、前端、wheel、Ubuntu 归档和 Compose 就绪检查通过。
- V0.4.2 Release 的资产摘要与本地归档 SHA256 一致；隔离 Web/API 可访问。
