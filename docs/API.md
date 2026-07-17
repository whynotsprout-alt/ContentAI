# ContentAI API 清单

当前公开契约固定为 35 个业务/运维端点。除健康检查、注册、验证、登录、忘记密码和重置密码外，端点均要求登录 Cookie；修改状态的登录端点还要求 `X-CSRF-Token`。FastAPI 交互文档位于 `/docs`，机器可读定义位于 `/openapi.json`，二者不计入 35 个端点。

## 系统（2）

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/health` | 无 | Compose、外部监控 | 无 / 基础状态 | 进程存活检查 |
| GET | `/api/ready` | 无 | Compose、运维 | 无 / 数据库、迁移、Redis、队列、outbox 检查 | 就绪检查 |

## 认证（9）

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| POST | `/api/auth/register` | 无 | AuthView | email、password / 提示消息 | 注册后可立即登录 |
| POST | `/api/auth/verify-email` | 无 | 无（兼容接口） | 任意请求 / `410 Gone` | 已停用的旧验证接口 |
| POST | `/api/auth/resend-verification` | 无 | 无（兼容接口） | 任意请求 / `410 Gone` | 已停用的旧验证重发接口 |
| POST | `/api/auth/login` | 无 | Auth Store | email、password / 当前用户 + Cookie | 登录 |
| POST | `/api/auth/logout` | 登录 + CSRF | Auth Store | 无 / 204 | 退出并撤销会话 |
| GET | `/api/auth/me` | 登录 | Auth Store | 无 / 当前用户 | 恢复登录态 |
| POST | `/api/auth/forgot-password` | 无 | 无（兼容接口） | 任意请求 / `410 Gone` | 已停用的密码重置接口 |
| POST | `/api/auth/reset-password` | 无 | 无（兼容接口） | 任意请求 / `410 Gone` | 已停用的密码重置接口 |
| POST | `/api/auth/change-password` | 登录 + CSRF | App | current_password、new_password / 提示消息 | 修改当前用户密码 |

## 内容账号 Agent（6）

Agent 仅属于当前 `user_id`。创建请求包含 `name`、`description`、`topic_scoring_prompt`、`content_prompt` 和 `hotspot_sources`；更新账号只修改名称/描述，提示词和热点源通过新版本修改。

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/agents` | 登录 | Workbench | 无 / 账号列表 | 列出当前用户账号 |
| GET | `/api/agents/{agent_id}` | 登录 | Workbench、AgentManager | 无 / 账号及当前版本 | 查看账号 |
| POST | `/api/agents` | 登录 + CSRF | AgentManager | 内容账号字段 / 账号 | 创建账号与首版本 |
| PATCH | `/api/agents/{agent_id}` | 登录 + CSRF | AgentManager | name、description（可选）/ 账号 | 修改账号资料 |
| POST | `/api/agents/{agent_id}/versions` | 登录 + CSRF | AgentManager | 三个版本配置字段 / 账号版本 | 固化新版本 |
| DELETE | `/api/agents/{agent_id}` | 登录 + CSRF | AgentManager | 无 / 204 | 删除未被会话引用的账号 |

## 会话与执行（9）

消息持久化仅允许用户/助手和 text/markdown。发送消息返回轻量提交结果；取消、恢复和状态接口返回轻量执行状态，不返回消息或记忆副本。SSE 使用 V3 事件，并支持 `Last-Event-ID` 或 `after_sequence` 断线续传。

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/chat/sessions` | 登录 | Workbench | 无 / 会话摘要列表 | 列出会话 |
| POST | `/api/chat/sessions` | 登录 + CSRF | Workbench | agent_id / 会话详情 | 创建并固定 AgentVersion |
| GET | `/api/chat/sessions/{session_id}` | 登录 | Workbench | cursor、limit / 会话、消息、最新执行 | 获取会话 |
| DELETE | `/api/chat/sessions/{session_id}` | 登录 + CSRF | Workbench | 无 / 204 | 删除会话及关联数据 |
| POST | `/api/chat/sessions/{session_id}/messages` | 登录 + CSRF | Workbench | agent_id、message、可选幂等键 / 提交结果 | 发起一轮执行 |
| GET | `/api/chat/runs/{execution_id}/events` | 登录 | Workbench SSE | 游标 / `text/event-stream` | 回放与跟随执行事件 |
| GET | `/api/chat/runs/{execution_id}/status` | 登录 | Workbench | 无 / 轻量执行状态 | 轮询执行状态 |
| POST | `/api/chat/runs/{execution_id}/cancel` | 登录 + CSRF | Workbench | 无 / 轻量执行状态 | 请求取消 |
| POST | `/api/chat/runs/{execution_id}/resume` | 登录 + CSRF | Workbench | agent_id、message / 轻量执行状态 | 恢复等待输入的执行 |

## 管理后台（9）

全部端点要求管理员身份。角色更新只接受 `role: "user" | "admin"`；服务端禁止管理员自降级，并保护最后一个有效管理员。

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/users` | 管理员 | AdminUsersView | 搜索、状态、分页 / 用户列表 | 用户检索 |
| GET | `/api/admin/users/{user_id}` | 管理员 | AdminUsersView | 无 / 用户详情 | 查看用户 |
| POST | `/api/admin/users/{user_id}/enable` | 管理员 + CSRF | AdminUsersView | 无 / 用户详情 | 启用用户 |
| POST | `/api/admin/users/{user_id}/disable` | 管理员 + CSRF | AdminUsersView | 无 / 用户详情 | 禁用用户 |
| POST | `/api/admin/users/{user_id}/password-reset` | 无（兼容接口） | 无 | 任意请求 / `410 Gone` | 已停用的密码重置接口 |
| PATCH | `/api/admin/users/{user_id}` | 管理员 + CSRF | AdminUsersView | role / 用户详情 | 升级或降级角色 |
| GET | `/api/admin/users/{user_id}/sessions` | 管理员 | AdminUsersView | 分页 / 会话摘要 | 查看用户会话 |
| GET | `/api/admin/sessions/{session_id}` | 管理员 | AdminUsersView | 无 / 审计消息 | 审计会话正文 |
| GET | `/api/admin/usage` | 管理员 | AdminUsersView | 用户、时间范围、粒度 / 用量桶 | 用量统计 |

## 契约约束

- `apps/api/tests/test_api_contract.py` 比较实际 OpenAPI 与上述 35 个端点，新增、删除或改变方法必须同步修改本文件和测试。
- 前端客户端分为 `api`、`authApi`、`adminApi`；每个方法必须存在页面、Store、测试或运维调用方。
- 仓库外调用方不在兼容范围内；本版本只支持最新契约和全新数据库安装。
