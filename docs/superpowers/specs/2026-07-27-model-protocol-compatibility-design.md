# 模型协议兼容与热点评分修复设计

## 背景与目标

当前 active 模型端点能够完成普通 OpenAI-compatible chat completion，但没有稳定遵循原生 `tool_calls` 与 `response_format=json_schema` 协议：主模型会把工具调用写成 XML 文本，结构化评分请求会返回说明性普通文本。结果是工具协议内容被当作 Assistant 消息流式展示，热点采集成功后又因评分结果无法通过 Pydantic 校验而失败。

本设计在不更换、不禁用当前端点的前提下完成以下目标：

- 原生兼容端点继续使用原生工具调用、结构化输出和实时流式输出。
- 部分兼容端点自动切换到受约束的文本工具调用和 JSON 结构化回退路径。
- 工具协议文本不得作为 Assistant 消息进入 SSE 或持久化消息。
- 热点评分失败必须成为真实的工具失败，审计状态不得记为 completed。
- 管理员 probe 明确展示端点的工具调用与结构化输出能力，但能力不足不阻止保存。
- 不增加数据库字段，不要求重建当前数据库，现有 active 配置原地继续生效。

## 方案选择

采用能力自适应兼容层。仅加强提示词无法可靠阻止协议文本泄漏；统一关闭流式或放弃原生协议会降低标准端点体验；持久化能力字段又会与当前单初始迁移基线和已运行数据库冲突。因此能力结果采用进程内、按不可变 `model_config_id` 缓存，并在缓存缺失时通过一次无副作用合成请求探测。

管理员 probe 与运行时探测使用相同的协议判定规则，但二者不共享持久化状态：管理员 probe 用于提前告警，运行时探测用于安全选路。Worker 重启后最多重新探测一次，不影响业务数据。

## 架构

### 1. 能力模型与缓存

在 LLM 基础设施层增加小型能力模型，至少包含：

- `native_tool_calls: bool`
- `json_schema: bool`
- 对应稳定警告码列表

运行时缓存以 `model_config_id` 为键，生命周期与 Worker 进程一致。缓存不得包含 API Key、响应正文或用户内容。并发首次访问同一配置时只允许一个探测请求，其余调用复用同一结果。HTTP 成功但协议结果不合格时进入安全兼容路径；网络、鉴权、限流和服务端异常不写入“不支持”缓存，直接沿用现有可重试或终止错误。

### 2. 主 Agent 工具调用

首次使用带工具的 Agent 模型前，能力探测向同一模型发送一个无副作用、强制调用的合成工具，要求复制固定 nonce：

- 返回合法原生 `tool_calls` 且参数 nonce 正确，判定支持原生工具调用。
- 返回 XML、普通文本、缺失/错误参数或不可解析响应，判定不支持。
- 网络、鉴权、限流和服务端异常按现有运行时错误处理，不降级为协议不支持。

原生路径保留现有 LangChain 工具绑定与实时 token SSE。兼容路径关闭该次模型调用的 LangGraph token callback，等待完整响应后使用严格的文本工具调用解析器归一化为 `AIMessage.tool_calls`，再交给图路由；因此 XML、`function_calls`、`invoke`、`system_warning` 和伪造的 `function_results` 都不会进入 Assistant SSE。兼容解析每轮最多接受一个当前注册工具调用，只允许该工具 schema 中的字段；未知工具或多个工具块使整次兼容解析失败，未知参数被丢弃且不会执行。

如果兼容路径最终得到普通 Assistant 回复而不是工具调用，完整回复仍通过正常的 Assistant 完成事件交付；原生兼容端点的普通回复继续保持实时流式。项目不伪造 token 级时间间隔。

### 3. 结构化输出回退

结构化模型包装器执行以下状态机：

1. 能力已知支持 `json_schema` 时使用原生 `with_structured_output`。
2. 能力未知时先尝试原生结构化请求；成功后缓存支持。
3. 仅当 HTTP 请求成功、但响应缺少结构化载荷或发生 JSON/Pydantic 解析错误时，缓存为不支持并执行一次文本 JSON 回退。
4. 网络、超时、鉴权、限流和 5xx 不触发协议回退，继续沿用现有错误分类与重试策略。

文本 JSON 回退把目标 Pydantic JSON Schema 作为独立系统约束发送，明确要求仅返回一个 JSON object。解析器允许 Markdown JSON fence 或对象前后的简短说明文字，但必须只提取出一个完整 JSON object；多对象、截断 JSON、尾随第二个对象或无法通过目标 Pydantic 类型校验的结果全部失败。业务层现有的候选 ID 白名单和排序约束继续生效。

回退只重试一次，防止无限模型循环和重复费用。失败异常使用稳定内部码 `MODEL_STRUCTURED_OUTPUT_INVALID`，不得记录远端响应正文。

### 4. 热点评分失败语义

`fetch_hotspots` 继续把单个来源异常放入来源诊断；来源部分失败但仍有候选时可以评分。评分模型不可用、结构化输出两条路径均失败或评分结果非法时，不再返回 `filtering.status=error` 的成功字典，而是抛出稳定的工具异常。

现有 `execute_tool_call` 负责把异常转换为 `ToolMessage(status="error")`、将 `ToolExecution.status` 置为 failed，并只暴露安全错误文案。主 Agent 可以解释失败或建议重试，但审计数据必须准确反映工具失败。成功结果仍返回 `filtering.status=ok` 和最终排序文本。

### 5. 管理员 probe 与界面

`OpenAICompatibleProbe` 保留 `/models` 和普通 completion 检查，并增加两个有界、无副作用的能力检查：

- 原生工具调用探测。
- 最小 JSON Schema 输出探测。

probe 响应增加明确的能力布尔值和稳定 warnings，例如 `MODEL_NATIVE_TOOL_CALLS_UNSUPPORTED`、`MODEL_JSON_SCHEMA_UNSUPPORTED`。普通 completion 可用但关键能力不足时 HTTP 仍返回 200，允许管理员保存；管理员模型配置页显示“将启用兼容模式”的非阻塞警告，而不是笼统成功或错误。警告需要可读文本、不能只靠颜色表达，并使用现有响应式和设计 token。

保存时服务端仍重新执行完整 probe。运行时不信任浏览器提交的能力字段，也不把 probe 响应持久化到 `ModelConfiguration`。

## 数据流

### 文本化工具调用

1. Worker 为 execution 构建模型网关。
2. 能力缓存 miss 时执行一次合成工具探测。
3. 当前端点被判定为非原生工具调用端点。
4. 实际 Agent 调用禁用 token callback，取得完整 XML 响应。
5. 严格解析器生成合法 `tool_calls` 并清空 Assistant content。
6. LangGraph 路由到 `fetch_hotspots`，SSE 只显示工具进度，不显示 XML。

### 热点评分

1. 热点来源采集并规范化候选。
2. 原生 JSON Schema 调用返回 HTTP 200 普通文本，解析失败。
3. 结构化包装器切换到带显式 schema 的文本 JSON 请求。
4. 单一 JSON object 通过 Pydantic 校验后进入现有候选 ID 校验和渲染。
5. 若回退仍失败，工具审计标记 failed，并把安全错误 ToolMessage 交回图。

## 安全与边界

- 合成探测工具不得访问业务工具、数据库或外部副作用系统。
- 探测 nonce 必须随机且仅用于验证回包关联，不写日志。
- 兼容解析器不得执行模型返回的未知工具、未知参数或嵌套协议文本。
- JSON 回退只接受一个顶层 object，并继续受 Pydantic 字段、长度和数值约束保护。
- 日志、审计和异常不得包含 API Key、Authorization、密文或远端响应正文。
- 所有探测与回退遵守现有 timeout、max_retries、token 上限、取消检查和使用量统计。

## 测试策略

所有实现按 TDD 完成，至少覆盖：

- 文本工具 XML 被归一化，但没有任何 `assistant_message_delta` 含协议标签或伪造结果。
- 原生工具调用端点仍产生工具进度并保留普通回复流式行为。
- 未知工具、错误 nonce 和多个文本工具块被拒绝；未知参数被丢弃且不会执行。
- 原生 JSON Schema 成功时不调用回退。
- HTTP 200 普通文本触发一次 JSON 回退并得到合法 `HotspotFilterResult`。
- fenced JSON、前后说明文字、截断 JSON、多个对象和 Pydantic 校验失败。
- 网络、鉴权、429、5xx 不错误切换为协议回退。
- 两条结构化路径均失败时，`ToolMessage.status=error` 且 `ToolExecution.status=failed`。
- probe 对完全兼容和部分兼容端点返回正确 capability/warnings，同时仍允许保存部分兼容配置。
- 管理员界面以可访问、响应式的非阻塞警告展示兼容模式。
- 当前相关 API、Worker、图执行和 Web 回归测试继续通过。

## 非目标

- 不把 ContentAI 改造成任意供应商协议转换网关。
- 不支持无法完成普通 chat completion 的端点。
- 不持久化供应商能力或远端响应正文。
- 不无限重试结构化结果，不尝试修复任意自然语言为业务数据。
- 不改造热点来源采集、账号评分规则或推荐排序逻辑。

## 验收标准

- 使用当前端点执行“获取今日热点”时，页面不再显示 XML/工具协议文本。
- 在当前端点忽略 `json_schema` 的情况下，合法文本 JSON 回退能够生成排序结果。
- 回退失败时用户得到稳定、可操作的失败说明，工具审计状态为 failed。
- 管理员 probe 明确显示兼容模式警告，且配置仍可保存和使用。
- 标准原生兼容测试端点不进入回退路径，普通回复保留实时 SSE。
- 不新增数据库迁移或要求重建现有数据库。
