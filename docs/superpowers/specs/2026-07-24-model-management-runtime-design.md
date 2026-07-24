# ContentAI 模型管理统一运行配置设计

## 背景

当前模型管理只保存 OpenAI 兼容服务的 Base URL、API Key 和模型 ID。Temperature、上下文窗口、对话最大输出和结构化输出最大值仍由 `CONTENTAI_LLM__*` 环境变量及全局 `LLMSettings` 提供。这使管理页面展示的“当前模型配置”并不是一次完整、可复现的运行时配置，也会让同一个模型配置版本在环境变量变化后产生不同执行结果。

当前开发环境还启用了代理 Fake-IP DNS。公开域名 `simpletest.up.railway.app` 被解析到 RFC 2544 基准测试保留网段 `198.18.0.0/15`，现有 SSRF 校验在发起请求前拒绝该地址，因此“刷新模型”“测试连接”和“保存并启用”均失败。

本轮按全新系统处理，不迁移、不回填也不兼容任何旧模型配置或旧数据库。数据库使用新的空卷重新初始化。

## 目标

- 让模型管理成为全部模型连接参数和模型运行参数的唯一配置来源。
- 让每个执行绑定一个不可变的完整模型配置版本，执行结果不受后续配置或环境变量变化影响。
- 删除全局 LLM 运行参数及对应 `.env` 覆盖项。
- 在模型管理页面完成配置、模型发现、连接验证、保存和启用。
- 直接允许当前代理使用的 `198.18.0.0/15` 网段，同时继续阻止其他高风险地址。
- 新数据库启动后自动创建 `.env` 指定的管理员账号。

## 非目标

- 不迁移、回填或读取旧模型配置。
- 不为旧数据库提供增量 Alembic 升级路径。
- 不从 `.env`、命令行或其他隐藏来源覆盖模型运行参数。
- 不开放结构化输出 Temperature；结构化调用继续固定为 `0`。
- 不改变 Agent 迭代次数、递归限制、工具超时、搜索、记忆、权限等非模型运行配置。
- 不放开除 `198.18.0.0/15` 之外的保留、回环、链路本地、元数据或组播地址。
- 不在应用启动代码中自动删除数据库或数据卷。

## 核心决策

### 强类型、不可变的模型配置版本

继续使用单一 `ModelConfiguration` 聚合，不增加 JSON 配置列或独立运行策略表。每个版本直接包含以下字段：

- `provider`：固定为 `openai_compatible`。
- `base_url`：OpenAI 兼容 API 根路径。
- `api_key_ciphertext`、`api_key_fingerprint`、`api_key_hint`：加密密钥及非敏感元数据。
- `model_name`：供应商返回或管理员手动输入的精确模型 ID。
- `temperature`：普通 Agent 对话的采样温度。
- `context_window_tokens`：模型上下文窗口。
- `chat_max_tokens`：普通对话最大输出。
- `structured_max_tokens`：结构化模型调用最大输出。
- 现有版本、启用状态、验证时间、创建人和审计字段。

每次“保存并启用”都新建完整版本并停用前一版本。历史版本不原地修改。API Key 未重新输入时，可以从当前生效版本继承密文；其他字段必须由本次完整请求明确提供。

### 参数约束

- `temperature` 必须在 `0` 到 `2` 之间，包含边界。
- `context_window_tokens`、`chat_max_tokens`、`structured_max_tokens` 必须为正整数。
- `chat_max_tokens < context_window_tokens`。
- `structured_max_tokens < context_window_tokens`。
- Base URL、API Key 和模型 ID 遵循现有长度、安全和必填约束。
- 首次配置必须提供 API Key；保存后的读取响应永不返回明文或可逆密文。

管理页面在空配置状态下可以预填建议值 `0.2 / 32000 / 8000 / 8000`，但这些值只有保存入库后才会成为运行配置。后端不得在运行时用代码默认值补齐缺失字段。

## 全新数据库基线

直接修改唯一的 V0.5 初始迁移 `202607210001_v050_initial_schema.py`，在创建 `modelconfiguration` 时一次性建立上述字段、非空约束和检查约束。ORM metadata 与初始迁移必须完全一致。

不新增增量迁移，不保留旧列兼容，不写数据回填逻辑。实施完成后由开发部署流程显式销毁当前开发数据库卷并从空数据库执行 `upgrade head`。应用自身不具备自动删库能力。

## API 与数据契约

模型配置读取、探测与更新请求扩展为强类型契约：

```python
class ModelRuntimeParameters:
    temperature: float
    context_window_tokens: int
    chat_max_tokens: int
    structured_max_tokens: int


class ModelConfigurationUpdateRequest(ModelRuntimeParameters):
    base_url: str
    api_key: str | None
    model_name: str
    expected_version: int
```

生效配置响应包含四个非敏感运行参数、版本和现有连接元数据，但不包含 API Key。前端 TypeScript 类型与后端契约保持一一对应。

“刷新模型”和“测试连接”使用当前表单中的 Base URL、API Key 和模型 ID，不需要持久化运行参数；“保存并启用”必须同时提交连接配置和完整运行参数。服务端先校验参数，再执行供应商探测，全部成功后才在一个事务中替换生效版本。探测或版本检查失败不得产生半成品记录。

审计记录可以包含所有非敏感字段和 API Key 指纹变化，但不得包含 API Key 明文或密文。

## 运行时数据流

```text
管理员保存并启用完整配置
        |
创建不可变 ModelConfiguration 版本
        |
新任务创建时记录 model_config_id
        |
RuntimeContainer 按该 ID 读取完整配置
        |
ModelGateway 使用同一版本构建全部模型客户端
        |
ContextAssembler 与输入预检使用同一版本的 token 预算
```

`RuntimeModelConfiguration` 扩展为携带四个运行参数。`ModelGateway` 不再接收或读取全局 `settings.llm`，而是直接使用执行绑定版本中的 Temperature 与输出限制。

上下文裁剪和用户输入预检必须从同一个 `model_config_id` 取得 `context_window_tokens` 与 `chat_max_tokens`。普通对话模型使用版本化 `temperature` 和 `chat_max_tokens`；热点筛选、研究结果等结构化模型固定 Temperature 为 `0`，使用版本化 `structured_max_tokens`。

已经创建的执行继续使用自己的 `model_config_id`。管理员启用新版本只影响之后创建的执行，不改变运行中任务或恢复任务的参数。

如果没有生效配置，创建模型任务应返回稳定的 `MODEL_NOT_CONFIGURED` 错误；前端显示“模型尚未配置”，管理员可直接进入模型管理。系统不得退回 `.env` 或代码默认模型。

## 环境变量与配置清理

从 `.env` 删除：

- `CONTENTAI_LLM__CHAT_MAX_TOKENS`
- `CONTENTAI_LLM__CONTEXT_WINDOW_TOKENS`
- `CONTENTAI_LLM__STRUCTURED_MAX_TOKENS`

同时删除 `LLMSettings`、`settings.llm` 校验、无引用常量、旧测试和文档说明。`.env.example` 不再展示任何模型运行参数。

保留 `CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY`。它是数据库内 API Key 的加密根密钥，不是模型运行参数；缺失或格式错误时服务必须拒绝启动。

保留：

- `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL`
- `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD`

这两个变量只负责新数据库的首次管理员引导，不参与模型配置。

## 启动管理员

数据库迁移完成后、API 对外就绪前，启动流程确保 `.env` 指定邮箱存在：

- 邮箱不存在时创建 `active`、`admin`、已验证且无需强制改密的用户。
- 密码按现有密码哈希策略入库，绝不记录或返回明文。
- 重复启动不重复创建，也不重置已有用户密码、角色或状态。
- 邮箱必须是有效格式；密码必须满足现有 `10–128` 字符登录密码约束，空白值无效。
- 邮箱与密码必须成对存在；新系统的运行配置要求二者均存在。
- 多实例并发启动遇到唯一约束竞争时读取最终记录并继续；若最终记录与有效管理员状态冲突，则明确失败，不静默提权或覆盖。

## Fake-IP 网络策略

在共享的模型端点地址校验中，将 `198.18.0.0/15` 作为明确、有限的允许网段。探测客户端、同步运行客户端和异步运行客户端必须复用同一规则。

该例外同时适用于域名解析结果和直接填写的 IP。它不应通过放宽通用 `is_reserved` 判断实现，而应使用显式 CIDR 判断，避免顺带允许其他保留网段。

网络边界保持如下：

- `198.18.0.0/15` 允许通过 HTTPS 访问。
- 公网及该保留网段的 HTTP 地址继续拒绝。
- `127.0.0.0/8`、`::1`、链路本地、云元数据地址、未指定地址、组播地址和其他保留地址继续拒绝。
- 现有企业内网 HTTP 兼容行为保持不变。
- 请求继续固定解析地址、Host 和 TLS SNI，关闭环境代理继承、自动重试和跨来源重定向。

## 模型管理界面

页面保持当前简约设计语言，表单划分为两个清晰区块：

1. 连接配置：Base URL、API Key、模型 ID，以及刷新模型操作。
2. 运行参数：Temperature、上下文窗口、对话最大输出、结构化输出最大值。

交互规则：

- 空配置时展示建议值和简短说明，不伪装为已生效配置。
- 已配置的 API Key 只显示不可逆提示；管理员留空表示沿用当前密钥，首次配置时必须填写。
- 模型候选仍允许刷新，也允许直接输入自定义模型 ID。
- 数值字段使用适合移动端的数字键盘、明确单位、边界说明和行内错误。
- 参数关系错误在客户端即时提示，服务端仍执行同样校验。
- “测试连接”不会保存；“保存并启用”在成功后刷新生效摘要。
- 版本冲突时保留管理员未提交内容，刷新当前版本信息并要求重新确认。
- 所有断点无横向滚动，键盘操作、焦点顺序、错误关联和触控目标满足现有全平台 UI 标准。

前端实施遵循 Impeccable：先取得项目上下文并检查现有设计系统，修改后执行适配、审计、打磨和加固验证。

## 错误处理

- 字段缺失或关系无效：`422`，返回可映射到字段的稳定错误。
- 端点仍被安全策略拒绝：`MODEL_ENDPOINT_FORBIDDEN`。
- 凭据无效：`MODEL_AUTH_FAILED`。
- 模型不存在：`MODEL_NOT_FOUND`。
- 供应商不可达：`MODEL_PROVIDER_UNREACHABLE`。
- 保存期间版本变化：现有版本冲突错误。
- 无生效配置：`MODEL_NOT_CONFIGURED`。
- 加密根密钥无效或管理员引导配置无效：启动失败，不带敏感值输出。

前端应展示可行动的中文反馈，不显示上游响应正文、API Key、密文、完整内部异常或堆栈。

## 测试与验收

实施使用 TDD，先建立失败测试，再完成最小实现。覆盖：

- ORM、初始迁移、检查约束和 Alembic metadata 一致性。
- 空数据库从 `base` 到 `head` 后包含完整模型配置结构。
- API 读取、探测、保存、版本冲突、密钥继承和秘密脱敏。
- 四个运行参数的边界与跨字段关系校验。
- 执行绑定旧版本、新执行使用新版本，以及恢复执行不漂移。
- Agent、结构化模型、上下文裁剪和输入预检均读取执行版本参数。
- `198.18.0.0/15` 边界地址允许；其他保留地址、元数据、回环和不安全 HTTP 仍拒绝。
- 同步与异步固定地址传输具有相同网络策略。
- 首次启动创建 `.env` 管理员、重复启动幂等、非法凭据失败、并发启动安全。
- 模型管理页面的空状态、字段校验、刷新模型、测试连接、保存、错误恢复和响应式布局。
- 负向搜索确认 `CONTENTAI_LLM__`、`settings.llm` 和废弃模型默认回退均已清零。

最终至少运行相关 API/前端测试、完整 `npm.cmd test`、`npm.cmd run build`、`npm.cmd run verify:web`、API 契约与迁移测试、`tools/review.ps1` 和 `git diff --check`。

随后显式重建开发数据库并启动项目，验证：

1. 空数据库成功初始化。
2. `.env` 管理员可登录。
3. 管理页面能保存截图中的服务地址和 `claude-opus-4-6`。
4. 刷新模型、测试连接、保存启用和真实最小调用成功。
5. 新对话使用数据库版本中的全部模型运行参数。
