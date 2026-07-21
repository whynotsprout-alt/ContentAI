# ContentAI V0.5.0 全新数据库基线设计

## 背景

当前分支的业务数据库结构由 V0.4 初始迁移和两段 V0.5 增量迁移共同构成。V0.5 开发期间又向已存在的父 revision 增加了字段，导致已经执行过后续 revision 的数据库无法通过常规 `upgrade head` 获得完整结构。

本版本不再支持任何旧数据库原地升级。V0.4.3、V0.5 开发版本以及其他已有 Alembic stamp 的数据库均不属于支持范围，部署时必须使用新的空数据库或 PostgreSQL 数据卷。当前最终 ORM 模型、API 合约和业务行为保持不变。

## 目标

- 用一份全新的 V0.5 初始迁移完整创建当前最终业务数据库结构。
- 保留当前 ORM 的表、字段、索引、约束、租户关系和时间语义。
- 让旧 revision 明确失败，避免把结构不完整的旧数据库误判为最新版本。
- 保持 LangGraph checkpoint 和 store 表由 LangGraph 自身管理。
- 为全新安装、降级往返、metadata 一致性和失败边界提供自动化验证。

## 非目标

- 不迁移、回填或保留旧数据库中的业务数据。
- 不改变 API、业务流程、ORM 字段语义或表间关系。
- 不在应用、迁移服务或部署脚本中自动删除数据库或数据卷。
- 不为旧 revision 提供兼容桥接迁移。
- 不把 LangGraph 自管表纳入业务 Alembic metadata。

## 迁移架构

仓库只保留一份业务迁移：

- 文件：`202607210001_v050_initial_schema.py`
- `revision = "202607210001"`
- `down_revision = None`

现有 `202607150001`、`202607170001` 和 `202607170002` revision 全部删除。新迁移直接创建当前 ORM 所需的最终表结构，不包含旧表检查、数据回填、旧 token 清理或增量列变更。

迁移中的表、外键、复合唯一约束、检查约束、索引、服务运行表和 UTC 时间字段必须与当前 SQLAlchemy metadata 一致。`useractiontoken` 等已从最终模型移除的旧结构不得进入新基线。

降级按依赖关系逆序删除 ContentAI 业务表。LangGraph 的 `checkpoints`、`checkpoint_*`、`store` 和 `store_migrations` 等外部管理表既不由升级创建，也不由降级删除。

## 部署与失败边界

支持的部署流程为：

1. 准备空 PostgreSQL 数据库或新数据卷。
2. migration 服务执行 `upgrade head`。
3. readiness 校验 Alembic revision、业务表和其他运行依赖。
4. API、Dispatcher 和 Worker 在 migration 成功后启动。

旧数据库不得直接执行新版本的 `upgrade head`。由于旧 revision 文件已移除，Alembic 遇到旧 stamp 时应无法解析当前 revision，并在修改业务结构前失败。运维文档必须明确说明本版本只支持新环境部署，并删除 V0.4.3 原地升级承诺。

系统不提供自动删库或自动清卷逻辑。数据销毁由运维人员在部署边界外显式完成，从而避免配置错误导致不可逆的数据删除。

## 测试设计

实施遵循 TDD：先让新的迁移契约测试在旧三段迁移结构下按预期失败，再创建新基线。

自动化验收覆盖：

- 仓库只存在一个业务 revision、一个 Alembic head，且 `down_revision = None`。
- 真实空 PostgreSQL 能从 `base` 升级到 `head`。
- 升级后的表、字段、外键、唯一约束、检查约束、索引和 UTC 时间类型符合 ORM 合约。
- `alembic check` 不报告 metadata drift。
- `head -> base -> head` 往返成功，且不误删 LangGraph 自管表。
- 人工写入旧 revision stamp 后，升级明确失败且不修改已有业务表。
- readiness 和相关契约中的期望 head 更新为 `202607210001`。

本轮同时修复 Task 4 reviewer 已确认的 research 测试夹具问题。测试 mock 必须返回带 package ID、topic hash、有效 HTTP(S) source、引用该 source 的 supported claim 以及稳定 claim ID 的完整资料包，不能通过弱化生产证据校验来恢复测试。

验证范围包括迁移契约、research、execution outbox 等受影响测试，以及 Ruff、compileall、Alembic metadata check 和 `git diff --check`。完整后端测试若无法在合理时间内取得终态，只记录可验证的实际结果，不声称全量通过。

## 交付与审查

数据库基线重构与 research 夹具修复使用独立提交，便于定位和回退。实现完成后生成完整 Task 4 review package，由独立 reviewer 同时检查规格符合性和代码质量。只有 Critical 与 Important 问题全部关闭后，Task 4 才能写入持久进度台账并进入下一项任务。
