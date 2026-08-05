# ContentAI API 清单

当前公开契约固定为 35 个业务/运维端点。除健康检查、就绪检查、注册和登录外，端点均要求登录 Cookie；修改状态的登录端点还要求 `X-CSRF-Token`。FastAPI 交互文档位于 `/docs`，机器可读定义位于 `/openapi.json`，二者不计入 35 个端点。

## 系统（2）

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/health` | 无 | Compose、外部监控 | 无 / 基础状态 | 进程存活检查 |
| GET | `/api/ready` | 无 | Compose、运维 | 无 / 数据库、迁移、Redis、队列、outbox 检查 | 就绪检查 |

## 认证（5）

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| POST | `/api/auth/register` | 无 | AuthView | email、password / 提示消息 | 注册后可立即登录 |
| POST | `/api/auth/login` | 无 | Auth Store | email、password / 当前用户 + Cookie | 登录 |
| POST | `/api/auth/logout` | 登录 + CSRF | Auth Store | 无 / 204 | 退出并撤销会话 |
| GET | `/api/auth/me` | 登录 | Auth Store | 无 / 当前用户 | 恢复登录态 |
| POST | `/api/auth/change-password` | 登录 + CSRF | App | current_password、new_password / 提示消息 | 修改当前用户密码 |

## 内容账号 Agent（6）

Agent 仅属于当前 `user_id`。创建请求包含 `name`、`description`、`topic_scoring_prompt`、`content_prompt` 和 `hotspot_sources`；更新账号只修改名称/描述，提示词和热点源通过新版本修改。账号列表按 `(created_at DESC, id DESC)` 稳定排序；更新账号不会再将它置顶。分页 cursor 是不透明签名值，并同时绑定当前用户和允许访问的 Agent 范围；篡改、跨用户或跨权限范围重放均返回 `422 INVALID_CURSOR`。

这是预发布阶段的 breaking change：`GET /api/agents` 从数组响应改为 `{items,next_cursor}`，且列表项只保留轻量摘要。需要提示词或热点源的调用方必须再读取 `GET /api/agents/{agent_id}` 详情。

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/agents` | 登录 | Workbench | 可选 cursor、limit / `{items,next_cursor}` 轻量账号摘要页 | 分页列出当前用户账号；摘要仅含当前版本 id/版本号，提示词与热点源由详情接口返回 |
| GET | `/api/agents/{agent_id}` | 登录 | Workbench、AgentManager | 无 / 账号及当前版本 | 查看账号 |
| POST | `/api/agents` | 登录 + CSRF | AgentManager | 内容账号字段 / 账号 | 创建账号与首版本 |
| PATCH | `/api/agents/{agent_id}` | 登录 + CSRF | AgentManager | name、description（可选）/ 账号 | 修改账号资料 |
| POST | `/api/agents/{agent_id}/versions` | 登录 + CSRF | AgentManager | 三个版本配置字段 / 账号版本 | 固化新版本 |
| DELETE | `/api/agents/{agent_id}` | 登录 + CSRF | AgentManager | 无 / 204 | 删除未被会话引用的账号 |

## 会话与执行（9）

消息持久化仅允许用户/助手和 text/markdown。发送消息返回轻量提交结果；取消、恢复和状态接口返回轻量执行状态，不返回消息或记忆副本。SSE 使用 V3 事件，并支持 `Last-Event-ID` 或 `after_sequence` 断线续传。非空 `Last-Event-ID` 始终优先于 query 中的 `after_sequence`；header 缺失或仅含空白时才解析 query。合法非空 header 会完全忽略 malformed 或负数 query；只有实际回退 query 时，`after_sequence` 才必须能解析为非负整数。带 execution ID 的 header 必须与路径中的 `execution_id` 一致，否则即使 query 合法也返回 422。

events 路由的 execution scope 与 cursor preflight 使用 function-scope 数据库 Session，并在返回 `StreamingResponse`、开始读取 body 前释放；后续 replay 只保留 database bind，并由 replay generator 为每次数据库访问创建自身的短 Session，不持有默认 request-scope Session 横跨长连接。

事件接口的 200 OpenAPI media schema 是实际传输类型 `string`，并分别给出 data、heartbeat 与 transport-control SSE 文本示例。可机读的 `StreamEventV3` 组件仅描述带 sequence/id 的 data frame，并按 `channel` 区分非 errors 的 `StreamPublicEventV3` 与 `channel=errors` 的 `StreamErrorEventV3`；后者的 data 强制包含非空 `code` 和 `message`，可含 `name`、`retryable` 及投影后允许公开的额外字段。无 sequence/id 的 transport control 不属于该 union。

只有 data frame 带 `id:` 并推进客户端的 Last-Event-ID；heartbeat 和 transport-control frame 均不带 `id:`。投影失败或未知 transport 异常会发送固定 `STREAM_EXCEPTION_ERROR`、不含原始 payload 的终止 control error，随后立即关闭流，不再发送后续事件。接口明确声明 401、403、404、409 和 422；409 的稳定错误码包括 `INVALID_STREAM_CURSOR`、`STREAM_REPLAY_GAP`、`STREAM_REPLAY_EXPIRED` 与 `STREAMING_DEGRADED`。422 同时覆盖 FastAPI 自动产生的 `HTTPValidationError` 数组和 `{"detail":"..."}` 字符串 detail；后者用于非法 fallback query 及 `Last-Event-ID` 与路径 execution 不匹配等手工校验。

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/chat/sessions` | 登录 | Workbench | 可选 agent_id、cursor、limit / `{items,next_cursor}` 会话摘要列表 | 筛选并分页列出会话 |
| POST | `/api/chat/sessions` | 登录 + CSRF | Workbench | agent_id / 会话详情 | 创建并固定 AgentVersion |
| GET | `/api/chat/sessions/{session_id}` | 登录 | Workbench | cursor、limit / 会话、消息、最新执行 | 获取会话 |
| DELETE | `/api/chat/sessions/{session_id}` | 登录 + CSRF | Workbench | 无 / 204 | 删除会话及关联数据 |
| POST | `/api/chat/sessions/{session_id}/messages` | 登录 + CSRF | Workbench | message、可选幂等键 / 提交结果 | 发起一轮执行 |
| GET | `/api/chat/runs/{execution_id}/events` | 登录 | Workbench SSE | 游标 / `text/event-stream` | 回放与跟随执行事件 |
| GET | `/api/chat/runs/{execution_id}/status` | 登录 | Workbench | 无 / 轻量执行状态 | 轮询执行状态 |
| POST | `/api/chat/runs/{execution_id}/cancel` | 登录 + CSRF | Workbench | 无 / 轻量执行状态 | 请求取消 |
| POST | `/api/chat/runs/{execution_id}/resume` | 登录 + CSRF | Workbench | interrupt_id、decision (`approve`/`reject`) / 轻量执行状态 | 恢复等待输入的执行 |

## 管理后台（13）

全部端点要求管理员身份。角色更新只接受 `role: "user" | "admin"`；服务端禁止管理员自降级，并保护最后一个有效管理员。

| 方法 | 路径 | 认证 | 调用方 | 请求 / 响应 | 用途 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/users` | 管理员 | AdminUsersView | 搜索、状态、分页 / 用户列表 | 用户检索 |
| GET | `/api/admin/users/{user_id}` | 管理员 | AdminUsersView | 无 / 用户详情 | 查看用户 |
| POST | `/api/admin/users/{user_id}/enable` | 管理员 + CSRF | AdminUsersView | 无 / 用户详情 | 启用用户 |
| POST | `/api/admin/users/{user_id}/disable` | 管理员 + CSRF | AdminUsersView | 无 / 用户详情 | 禁用用户 |
| POST | `/api/admin/users/{user_id}/temporary-password` | 管理员 + CSRF | AdminUsersView | 无 / 一次性临时密码、过期时间 | 签发临时密码；用户随后只能查看自身、退出或修改密码 |
| PATCH | `/api/admin/users/{user_id}` | 管理员 + CSRF | AdminUsersView | role / 用户详情 | 升级或降级角色 |
| GET | `/api/admin/users/{user_id}/sessions` | 管理员 | AdminUsersView | cursor、limit / 会话摘要 | 查看用户会话 |
| GET | `/api/admin/sessions/{session_id}` | 管理员 | AdminUsersView | 无 / 会话审计摘要 | 查看会话审计摘要 |
| GET | `/api/admin/sessions/{session_id}/messages` | 管理员 | AdminUsersView | cursor、limit / 审计消息页 | 分页读取会话消息 |
| GET | `/api/admin/usage` | 管理员 | AdminUsersView | 用户、时间范围、粒度 / 用量桶 | 用量统计 |
| GET | `/api/admin/model-config` | 管理员 | AdminModelsView | 无 / active 配置元数据或 `configured: false` | 获取唯一全局 OpenAI-compatible 配置；不返回 API Key 或密文，且响应不缓存 |
| POST | `/api/admin/model-config/probe` | 管理员 + CSRF | AdminModelsView | 可选 api_mode、base_url、可选 api_key、可选 model_name / 标准化地址、候选模型、验证结果、延迟 | 测试候选；省略 api_mode 时默认 `chat_completions`；空 Key 只会在 endpoint 同 origin 时沿用当前 active Key，首次配置或跨 origin 均返回 `MODEL_CREDENTIALS_REQUIRED` |
| PUT | `/api/admin/model-config` | 管理员 + CSRF | AdminModelsView | base_url、model_name、temperature、context_window_tokens、chat_max_tokens、structured_max_tokens、expected_version；可选 api_mode、api_key、input_price_per_million_usd、output_price_per_million_usd / 新 active 配置元数据 | 保存前服务端重做完整 probe；首次配置或跨 origin 必须提供 Key，空 Key 仅可在同 origin 沿用当前 active Key；省略 api_mode 时已有配置沿用当前模式，新配置默认 `chat_completions` |

`api_mode` 只允许 `chat_completions` 或 `responses`。探测和运行时调用固定使用所选 endpoint，不再根据模型名称自动切换。

模型配置的稳定错误码为：`MODEL_NOT_CONFIGURED`（503）、`MODEL_CREDENTIALS_REQUIRED`（422）、`MODEL_ENDPOINT_FORBIDDEN`（422）、`MODEL_CONFIG_CHANGED`（409）、`MODEL_CONFIG_PERSISTENCE_FAILED`（503）、`MODEL_AUTH_FAILED`（422）、`MODEL_NOT_FOUND`（422）、`MODEL_PROVIDER_UNREACHABLE`（502）和 `MODEL_PROBE_FAILED`（502）。这些响应、模型配置请求校验错误和成功响应均不会回显 API Key、密文、Authorization 或远端响应正文。

## 契约约束

- `apps/api/tests/test_api_contract.py` 比较实际 OpenAPI 与上述 35 个端点，新增、删除或改变方法必须同步修改本文件和测试。
- 前端客户端分为 `api`、`authApi`、`adminApi`；每个方法必须存在页面、Store、测试或运维调用方。
- 仓库外调用方不在兼容范围内；本版本只支持最新契约和全新数据库安装。
- 未配置 active 模型时，`POST /api/chat/sessions/{session_id}/messages` 在创建消息、execution 或 outbox 前返回 `503 MODEL_NOT_CONFIGURED`。模型切换仅影响新 execution；已 queued、running、resume 或 retry 的 execution 使用固化的历史配置版本。
