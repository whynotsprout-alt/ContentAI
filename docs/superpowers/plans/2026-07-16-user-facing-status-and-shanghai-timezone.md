# 用户状态提示与上海时区 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留既有中文状态提示，移除状态栏后半段的候选和来源明细，并让用户可请求的当前时间固定为亚洲/上海时区。

**Architecture:** 前端仅缩减状态栏的辅助详情渲染，保留现有主状态文案、工具进度标签和错误处理。后端的 `current_datetime` 固定使用 `ZoneInfo("Asia/Shanghai")`，不再接受可变时区输入。

**Tech Stack:** Vue 3、Vitest、FastAPI/Python、pytest、`zoneinfo`。

## Global Constraints

- 保持现有主状态和错误提示文案不变。
- 状态栏不再显示候选数量、来源健康度和平台名称明细。
- `current_datetime` 固定使用 `Asia/Shanghai`；事件与数据库存储时间不在本次变更范围内。
- 不修改当前工作区中无关的未提交文件。

---

## File Structure

- `apps/web/src/components/RunActivityBar.vue`：保留主状态文案，仅移除候选和来源详情渲染。
- `apps/web/tests/frontend-plan.spec.mjs`：防止状态栏重新渲染候选和来源明细。
- `apps/api/src/agent/tools/system.py`：提供固定上海时区的当前时间工具。
- `apps/api/tests/test_system_tool.py`：覆盖固定时区和工具 schema。

### Task 1: 移除状态栏后半段的候选与来源明细

**Files:**
- Modify: `apps/web/src/components/RunActivityBar.vue`
- Modify: `apps/web/tests/frontend-plan.spec.mjs`

**Interfaces:**
- Consumes: 既有 `lifecycle`、`notice` 与 `events` props。
- Produces: 仅渲染原有主状态文案和状态图标的活动栏。

- [ ] **Step 1: 先写会失败的前端源码契约测试**

  在 `frontend-plan.spec.mjs` 增加：

  ```js
  it('keeps the primary status copy but hides activity details', async () => {
    const activity = await read('../src/components/RunActivityBar.vue');
    expect(activity).toContain('progressLabel');
    expect(activity).not.toContain('activity-detail');
    expect(activity).not.toContain('source-health');
    expect(activity).not.toContain('source_health');
    expect(activity).not.toContain('candidate_count');
  });
  ```

- [ ] **Step 2: 运行测试，确认因现有详情渲染而失败**

  Run: `npm test -- --run tests/frontend-plan.spec.mjs`

  Expected: FAIL；组件仍包含 `activity-detail`、`source-health`、`source_health` 和 `candidate_count`。

- [ ] **Step 3: 实现最小界面改动**

  在 `RunActivityBar.vue` 删除 `SourceHealth` 类型、`sourceHealth` 与 `toolDetail` 计算属性；从 lucide 导入中移除 `Clock3` 与 `Search`。保留 `activeEvent`、`toolName`、`toolProgress` 与 `toolLabel`，以维持已有主状态文案。删除模板中的以下三段详情内容：

  ```vue
  <span v-if="lifecycle === 'queued'" class="activity-detail">...</span>
  <span v-else-if="lifecycle === 'running' && toolDetail" class="activity-detail">...</span>
  <span v-if="sourceHealth.length" class="activity-detail source-health">...</span>
  ```

- [ ] **Step 4: 运行前端测试与构建，确认通过**

  Run: `npm test -- --run tests/frontend-plan.spec.mjs && npm run build`

  Expected: PASS，且 TypeScript 不报告未使用图标或类型错误。

- [ ] **Step 5: 提交任务改动**

  ```bash
  git add apps/web/src/components/RunActivityBar.vue apps/web/tests/frontend-plan.spec.mjs
  git commit -m "fix: hide run activity details"
  ```

### Task 2: 固定当前时间为亚洲/上海时区

**Files:**
- Modify: `apps/api/src/agent/tools/system.py:3-27`
- Create: `apps/api/tests/test_system_tool.py`

**Interfaces:**
- Consumes: `ToolRuntimeContext.can_use_tool("current_datetime")`。
- Produces: `current_datetime() -> dict[str, str]`，成功时返回固定 `timezone == "Asia/Shanghai"`、带 `+08:00` 偏移的 ISO 时间、日期与时间。

- [ ] **Step 1: 先写会失败的后端测试**

  新建 `test_system_tool.py`：

  ```python
  from datetime import datetime

  from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
  from agent.tools.system import current_datetime

  def test_current_datetime_is_always_shanghai_time() -> None:
      context = ToolRuntimeContext(
          execution_id="time-test", conversation_id="session-time-test",
          session_id="session-time-test", agent_id="agent", user_id="user",
          tool_policies={"current_datetime": {}},
      )
      with tool_runtime_scope(context):
          result = current_datetime.invoke({})

      assert result["timezone"] == "Asia/Shanghai"
      assert datetime.fromisoformat(result["iso"]).utcoffset().total_seconds() == 8 * 60 * 60
      assert result["date"] == result["iso"][:10]
      assert "timezone" not in current_datetime.args_schema.model_fields
  ```

- [ ] **Step 2: 运行测试，确认现有 schema 仍允许可变时区**

  Run: `pytest apps/api/tests/test_system_tool.py::test_current_datetime_is_always_shanghai_time -v`

  Expected: FAIL；现有公开工具 schema 仍提供 `timezone` 参数。

- [ ] **Step 3: 实现最小后端改动**

  将工具函数签名与时区逻辑改为：

  ```python
  SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")

  @tool("current_datetime", description=load_tool_description("current_datetime"))
  def current_datetime() -> dict[str, str]:
      if not get_tool_runtime_context().can_use_tool("current_datetime"):
          return {"error": "Tool is not allowed for this run.", "tool": "current_datetime"}
      now = datetime.now(SHANGHAI_TIMEZONE)
      return {
          "timezone": "Asia/Shanghai",
          "iso": now.isoformat(),
          "date": now.date().isoformat(),
          "time": now.strftime("%H:%M:%S"),
      }
  ```

  删除 `UTC` 导入及异常回退逻辑。

- [ ] **Step 4: 运行后端测试，确认通过**

  Run: `pytest apps/api/tests/test_system_tool.py -v`

  Expected: PASS，工具 schema 不含 `timezone`，且返回值为 `Asia/Shanghai` 与 `+08:00`。

- [ ] **Step 5: 运行相关回归测试**

  Run: `pytest apps/api/tests/test_tool_execution.py apps/api/tests/test_api.py -q`

  Expected: PASS。

- [ ] **Step 6: 提交任务改动**

  ```bash
  git add apps/api/src/agent/tools/system.py apps/api/tests/test_system_tool.py
  git commit -m "fix: use shanghai time for current datetime"
  ```

### Task 3: 完成交付验证

**Files:**
- Verify only: `apps/web/src/components/RunActivityBar.vue`
- Verify only: `apps/api/src/agent/tools/system.py`

**Interfaces:**
- Consumes: 两个任务的已提交实现。
- Produces: 可交付的前端构建与相关测试结果。

- [ ] **Step 1: 运行完整前端测试**

  Run: `npm test`

  Expected: PASS。

- [ ] **Step 2: 运行完整后端测试套件**

  Run: `pytest apps/api/tests -q`

  Expected: PASS。

- [ ] **Step 3: 审核工作区范围**

  Run: `git status --short && git log -2 --oneline`

  Expected: 本次仅包含上述两个功能提交；原有未提交文件保持不变。
