# ContentAI V0.4.1 升级脚本修复设计

## 目标

修复 V0.4 Ubuntu 归档中 `upgrade.sh` 无法调用同目录 `backup.sh` 与 `health.sh` 的问题，并以新的不可变版本发布修正后的部署包。

## 根因

Git 与 Ubuntu 归档中的运维脚本权限为 `0644`。文档以 `bash infra/ubuntu/upgrade.sh` 启动升级脚本，但脚本内部直接执行 `backup.sh` 和 `health.sh`，因此会在调用时收到 `Permission denied`。

## 决策

- 保持 V0.4 的标签、Release 与校验和不变。
- 在 `upgrade.sh` 中使用 `bash "$ROOT/infra/ubuntu/backup.sh"` 和 `bash "$ROOT/infra/ubuntu/health.sh"`，不依赖文件可执行位。
- 将包版本与 Ubuntu 归档更新为 `0.4.1`，Git 标签与 GitHub Release 使用现有惯例 `V0.4.1`。
- 增加归档后回归验证：将被调用脚本设为非可执行，确认升级脚本仍通过 Bash 调用路径进入后续逻辑。

## 验收

- 在脚本权限为 `0644` 时，`upgrade.sh` 不再因调用 backup/health 报 `Permission denied`。
- 全量后端、前端、构建、wheel 与 Ubuntu 归档验证通过。
- GitHub 上同时保留 V0.4 与新的 V0.4.1 Release，两个附件和 SHA256 均不可变且各自匹配。
