# ContentAI 前端功能设计文档

> 交接基线：`0.6.0`，更新日期：2026-07-27。本文只描述 `apps/web` 当前代码已经实现的行为；接口与服务端约束见 [后端功能设计文档](BACKEND_DESIGN.md)，运行方式见 [项目部署文档](DEPLOYMENT.md)。

## 1. 产品入口与技术边界

前端是 Vue 3 单页应用，使用 TypeScript、Vue Router、Pinia 和 Vite。生产构建由 Nginx 提供静态文件并把 `/api/` 反向代理到 FastAPI；本地 Vite 开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8000`。浏览器端不保存模型密钥、登录令牌或业务数据副本。

主要依赖：

| 能力 | 实现 |
| --- | --- |
| 页面与组件 | Vue 3 SFC |
| 路由 | Vue Router 4，History 模式 |
| 状态 | Pinia：`auth`、`workbench` |
| 图标 | Lucide Vue |
| Markdown | `markdown-it`，禁用原始 HTML、启用换行和链接识别 |
| HTML 清理 | DOMPurify |
| 测试 | Vitest；发布浏览器门禁使用 Playwright Core、axe-core |

应用入口依次为 `main.ts` → `Root.vue` → 路由页面。`Root.vue` 根据路由设置 `body[data-surface]`，并监听全局 `contentai:auth-expired` 事件，将失效身份清空后带安全的站内 `redirect` 跳转登录页。

## 2. 路由、权限与导航

| 路径 | 页面 | 访问规则 | 主要功能 |
| --- | --- | --- | --- |
| `/` | 重定向 | — | 跳转 `/app` |
| `/login` | `AuthView` | 公开；已登录用户跳工作台或强制改密页 | 邮箱密码登录 |
| `/register` | `AuthView` | 公开；已登录用户跳工作台或强制改密页 | 注册后跳登录 |
| `/change-password` | `ChangePasswordView` | 登录；只允许 `must_change_password=true` 用户停留 | 首次临时密码强制修改 |
| `/app` | `App` | 登录且已完成强制改密 | 内容账号与持续对话工作台 |
| `/admin/users` | `AdminUsersView` | 管理员 | 用户、角色、状态、用量、会话审计 |
| `/admin/models` | `AdminModelsView` | 管理员 | 全局模型连接与运行参数 |
| `/:pathMatch(.*)*` | 重定向 | — | 其他路径跳转 `/login` |

导航守卫先调用 `auth.ensureLoaded()` 请求 `/api/auth/me`，再执行以下规则：

1. 未登录访问私有页，跳到 `/login?redirect=<原路径>`。
2. 登录用户访问登录或注册页，按是否必须改密跳转 `/change-password` 或 `/app`。
3. 使用临时密码的用户只能进入改密页；改密完成后使用经过校验的站内目标返回。
4. 普通用户访问管理员页面，回到 `/app`。
5. `redirect` 必须以单个 `/` 开头、能被路由解析且不能再次指向改密页，防止开放重定向与循环跳转。

## 3. 认证与账号安全界面

### 3.1 登录和注册

`AuthView` 复用一套表单：

- 登录提交邮箱和密码；成功后恢复目标路由或进入 `/app`。
- 注册要求两次密码一致，浏览器端最少 10 字符、最多 128 字符；服务端注册成功后直接跳到登录页，不存在邮箱验证或重发验证入口。
- 密码字段提供显示/隐藏控制，使用正确的 `autocomplete` 属性。
- API 错误显示服务端消息；存在 `request_id` 时一并展示，便于排障。
- 登录页背景先加载静态海报，只有未启用“减少动态”、非省流量/2G、页面可见时才懒加载 MP4；视频失败或被自动播放策略阻止不影响表单。

### 3.2 修改密码

普通用户可从工作台账号菜单打开改密对话框；临时密码用户进入独立强制改密页。表单校验新密码至少 10 字符且两次输入一致，并阻止重复提交。成功后刷新当前身份；刷新失败时清空本地身份并跳登录，避免使用不确定会话。

服务端签发临时密码后会撤销旧会话。用户使用临时密码登录后，除查看自身、退出和修改密码外的 API 都会被服务端拒绝，前端路由也强制停留在改密页。

## 4. 对话工作台

### 4.1 页面布局

`App.vue` 组合以下区域：

| 区域 | 组件 | 职责 |
| --- | --- | --- |
| 顶栏 | `WorkbenchHeader` | 会话导航开关、品牌跳转、内容账号切换、账号管理、管理员入口、改密、退出 |
| 会话导航 | `SessionRail` | 新建、搜索、选择、分页加载、删除会话 |
| 消息区 | `ChatCanvas` | 引导态、消息渲染、复制、长回复展开、输入与停止 |
| 上下文侧栏 | `ConversationContextRail` | 当前账号/版本/来源数、会话标题、运行状态、消息数、更新时间 |
| 人工确认 | `InterruptApproval` | 查看整批待执行动作，批准全部、拒绝全部或取消运行 |
| 内容账号管理 | `AgentManager` | 创建、编辑、版本化配置、删除内容账号 |

工作台不展示全局运行活动条或固定“热点—选题—研究—写稿”阶段。生成反馈只保留在当前 Assistant 消息气泡内，所有内容结果仍作为普通用户/Assistant 对话消息展示。

### 4.2 启动、账号切换与会话选择

工作台启动流程：

1. 拉取当前用户的内容账号。
2. 没有账号时显示“财经解读”“AI 资讯”两个创建模板入口。
3. 有账号时选择当前账号（默认列表第一项），拉取其最近会话。
4. 有历史会话则加载最近一条；没有则自动创建新会话。

切换内容账号会立即开启新的“会话上下文代次”，关闭旧 SSE、清空旧消息/执行状态并只加载目标账号的会话。所有异步请求和 SSE 事件都带本地代次检查，旧账号、旧会话或旧 execution 的迟到响应不得覆盖当前界面。

会话列表按更新时间倒序显示，前端支持标题搜索；每页默认 50 条，通过有签名的 `next_cursor` 继续加载。当前会话使用 `aria-current="page"`。加载会话时显示服务端返回的最新 50 条可见消息；后端虽返回更早消息游标，当前普通工作台没有“加载更早消息”按钮。

删除会话必须经过应用内确认对话框。后端拒绝删除活跃或等待确认的会话时，前端保留原会话并提示先完成或取消运行；成功删除当前会话后自动切换到下一条，没有剩余会话则新建。

### 4.3 发送消息与运行状态

发送按钮仅在以下条件同时成立时可用：已有账号和会话、会话属于当前账号、不处于账号/会话切换中、运行不在 `queued/running/reconnecting/cancelling/waiting_input`。

桌面端 Enter 发送、Shift+Enter 换行；手机端 Enter 换行，只能点击发送按钮。输入法组合态不会误发送。空白输入不会创建占位消息。

一次提交的前端流程：

1. 再次确认会话的 `agent_id` 与当前选择一致。
2. 生成 `client-<UUID>` 幂等键。
3. 乐观加入用户消息和待响应 Assistant 占位气泡。
4. `POST /api/chat/sessions/{session_id}/messages` 仅发送 `message` 和幂等键。
5. 收到 `execution_id` 后订阅运行事件，并刷新会话列表。
6. 提交失败时移除乐观消息和空占位；未配置模型时显示“模型服务尚未配置，请联系管理员”。

前端运行生命周期与服务端状态映射：

| 前端状态 | 来源/含义 | UI 行为 |
| --- | --- | --- |
| `idle` | 无任务 | 可发送 |
| `queued` | 服务端 `pending` | 显示等待执行和 Assistant 占位 |
| `running` | 服务端 `running` 或收到运行/消息事件 | Assistant 气泡显示正在生成或流式内容 |
| `reconnecting` | SSE 断开或流降级 | 尝试续传或每 3 秒轮询 |
| `waiting_input` | 服务端等待人工确认 | 禁止新消息，显示确认面板 |
| `cancelling` | 已请求取消 | 关闭事件流并等待终态 |
| `completed` | 正常结束 | 重载消息和会话摘要 |
| `failed` | 执行或传输错误 | 移除空占位、展示稳定错误信息 |
| `cancelled` | 已停止 | 保留已经渲染的部分内容 |

停止运行时，前端关闭 SSE、调用取消接口，然后最多以 250ms 间隔查询会话 20 次；运行终止后保留已经生成的部分内容。

### 4.4 SSE、续传与降级

前端使用基于 `fetch` 的 `FetchEventStream`，支持 Cookie、CSRF 头和服务端 V3 事件格式。监听四个通道：`messages`、`lifecycle`、`interrupts`、`errors`。

- `assistant_message_delta` 逐块更新同一个 Assistant 气泡；`assistant_message` 写入最终内容。
- 复合事件 ID 中的序号用于去重和保存 `lastEventSequence`，最多保留最近 400 个本地去重键。
- 普通断线先查询运行状态，再以最后序号重订阅；退避从 1 秒指数增长到 10 秒，最多 8 次。
- `STREAMING_DEGRADED`、`STREAM_REPLAY_GAP`、`STREAM_REPLAY_EXPIRED`、`REDIS_READ_FAILED` 不直接判定业务执行失败，改为每 3 秒轮询 `/status`，终态后重载会话获取最终消息。
- 事件必须属于当前 session 和 execution；其他上下文事件直接丢弃。

### 4.5 消息显示

- 仅展示 `user/assistant` 且类型为 `text/markdown` 的消息。
- Markdown 禁止原始 HTML，生成结果再经 DOMPurify 清理。
- 外部链接新窗口打开并附加 `noopener noreferrer nofollow`，同时显示可解析域名信息。
- 超过 1800 字符的 Assistant 回复默认折叠，可展开/收起。
- Assistant 回复支持复制；剪贴板失败时提供可恢复的手动复制提示。
- 消息变化自动滚动到底部；关键终态通过 `aria-live` 宣告。

### 4.6 人工确认

当前实际需要确认的副作用工具是 `remember`。确认面板展示 `interrupt_id` 下全部操作的工具名、用途以及可选的记忆类型/内容。用户只能对整批操作“批准全部”或“拒绝全部”，也可取消整次运行。恢复请求只提交 `interrupt_id` 和 `decision`，不提交自由文本或 `agent_id`；失效/格式错误的 interrupt 会刷新会话并保持安全失败。

## 5. 内容账号管理

`AgentManager` 是不可点击背景关闭的全屏可访问对话框，桌面为目录/分区/编辑器三列，窄屏为目录与编辑器两步流。支持账号搜索、未保存状态提示、放弃更改确认和删除确认。

配置分四组：

| 分组 | 字段 | 前端要求 | 服务端语义 |
| --- | --- | --- | --- |
| 基础信息 | 名称、账号定位 | 均不能为空 | 名称/定位直接更新账号资料 |
| 热点来源 | 23 个来源多选 | 至少一个 | 进入新版本的 `hotspot_sources` |
| 评分规则 | 选题评分提示词 | 非空 | 只提供给隔离的热点评分子模型 |
| 内容规则 | 内容生成提示词 | 非空 | 进入会话系统提示，在允许写作时生效 |

创建账号会同时创建版本 1，并自动切换到该账号及其新会话。编辑时先 PATCH 名称/定位，再创建新版本保存提示词和来源；已有会话继续使用创建时固化的旧版本，新版本只影响之后新建的会话。

可选热点源共 23 个：36Kr、财联社、经济观察报、第一财经、虎嗅、界面、钛媒体、晚点 LatePost、量子位、雷峰网、财新、Vista 看天下、Financial Times、WSJ、TechCrunch、The Verge、爱范儿、证券时报、抖音、Bilibili、小红书、微博、AI HOT。

删除仅在账号没有会话、调用、执行或账号级记忆引用时成功。删除当前账号后自动选择下一账号；没有剩余账号时回到创建引导。

## 6. 用户管理后台

`/admin/users` 的功能包括：

- 按邮箱搜索（250ms 防抖）和按 `active/pending_verification/disabled` 状态筛选。
- 每页 50 条的签名游标分页；表格显示角色、状态、内容账号数、会话数和输入/输出/总 Token。
- 查看用户详情：验证状态、密码是否已设置、注册/最近登录、Token 统计完整度。
- 查看最近 7 个用量桶。
- 启用/禁用用户；不能禁用当前登录管理员，服务端保护最后一个有效管理员。
- 提升为管理员或降级为普通用户；不能自降级，服务端保护最后一个有效管理员。
- 为其他未禁用用户生成 24 小时临时密码。密码只在对话框显示一次，可复制；关闭、切换用户或请求失效后立即从页面状态清除。
- 只读会话审计：分页加载用户会话和其中的用户/Assistant 消息，不展示工具消息、checkpoint 或内部状态。

用户详情、会话、用量和审计请求使用 generation guard。用户快速切换、关闭审计或发起新请求后，旧响应和旧 `finally` 不得修改当前 loading、选中项或临时密码。

窄于 1280px 时用户列表与详情改为显式前进/返回；会话审计也改为索引/转录两步流，并恢复原滚动位置和键盘焦点。

## 7. 模型管理后台

`/admin/models` 维护唯一全局 OpenAI-compatible active 配置。页面能力：

1. 读取当前版本、端点、模型、Key 提示、运行参数、验证时间和操作者；永不接收或显示完整 Key。
2. “刷新模型”只 probe 连接并拉取候选模型；模型列表为空或被截断时仍允许输入自定义模型 ID。
3. “测试连接”同时验证选定模型的原生工具调用和 JSON Schema 能力。
4. “保存并启用”提交 Base URL、可选新 Key、模型 ID、`expected_version` 和全部运行参数；服务端会再次 probe。
5. 首次配置必须填写 Key；已有配置时留空表示沿用现有 Key。
6. Temperature 可选自动（提交 `null`，上游不发送该字段）或 `0..2` 数值。
7. 上下文窗口、对话输出、结构化输出必须为正整数，两个输出上限都小于上下文窗口；未配置页面建议值为 32000/8000/8000。
8. `MODEL_CONFIG_CHANGED` 时后台刷新 active 版本但保留当前草稿，要求管理员核对后重试；无法同步时禁用保存。
9. 所有 probe/save 都以规范化草稿签名和请求代次防止慢旧响应覆盖新输入。

保存的新版本只影响之后新建的 execution；页面明确提示已排队、运行、恢复和重试的任务继续使用原版本。

## 8. API 客户端与错误处理

`services/api.ts` 提供三组调用：

- `api`：Agent、会话、消息、运行、SSE。
- `authApi`：注册、登录、退出、当前用户、改密。
- `adminApi`：用户、审计、用量、模型配置。

所有普通请求使用 `credentials: include`，从 `contentai_csrf` Cookie 读取值并写入 `X-CSRF-Token`。`204` 返回 `undefined`。错误统一转换为 `ApiError`，保留 HTTP 状态、稳定错误码、可重试标志、`request_id/session_id/thread_id/execution_id`。

除登录/注册外，请求收到 401 或表示账号禁用的 403 时触发全局身份过期事件。模型错误会映射为中文可操作提示；其他错误尽量保留请求标识。

## 9. 响应式与无障碍约束

- 手机：`<=767px`。会话栏为带遮罩的模态抽屉，支持 Escape、Tab 焦点约束、关闭后焦点恢复；小于等于 360px 时占满屏宽。发送按钮为图标按钮，Enter 只换行。
- 平板：`768–1023px`。会话栏默认 72px 紧凑模式，可展开至 280px，并把选择保存在 `localStorage(contentai:session-rail-expanded)`。
- 小桌面：`1024–1279px`，会话栏 248px；中桌面 `1280–1439px` 为 264px；大桌面 `>=1440px` 为 280px，并显示右侧上下文栏。
- 可点击控制保证移动触控尺寸；支持 320px 宽度和浏览器 200% 缩放测试。
- `AccessibleDialog` 支持 Teleport、嵌套层级、只允许顶层关闭、焦点圈、背景 inert/恢复、Escape 和焦点回退；忙碌时禁止关闭。
- 工作台和管理页均有跳到主内容链接、语义化状态、键盘表格行、正确的 `aria-current/aria-selected/aria-busy/aria-live`。
- `prefers-reduced-motion` 会近乎关闭动画；`prefers-reduced-transparency` 改用不透明结构背景。

## 10. 前端文件职责与验证

| 路径 | 职责 |
| --- | --- |
| `src/router.ts`、`src/auth/foundation.ts` | 路由与认证守卫 |
| `src/services/api.ts` | HTTP/SSE 客户端和公开类型 |
| `src/stores/auth.ts` | 登录态 |
| `src/stores/workbench.ts` | 账号、会话、运行、SSE 恢复的核心状态机 |
| `src/App.vue` | 工作台编排与响应式会话导航 |
| `src/components/AgentManager.vue` | 内容账号编辑器 |
| `src/views/AdminUsersView.vue` | 用户和会话审计 |
| `src/views/AdminModelsView.vue` | 全局模型配置 |
| `src/styles/` | tokens、基础、认证、对话框、管理、产品响应式样式 |
| `tests/` | API 使用、跨会话、响应式、可访问性和发布门禁 |

交接后修改前端至少执行：

```powershell
Push-Location apps/web
npm.cmd test
npm.cmd run build
Pop-Location
```

完整仓库门禁见 [项目部署文档](DEPLOYMENT.md#4-测试与交付前验收)。
