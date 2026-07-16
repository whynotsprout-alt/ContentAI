# 展开回复按钮与会话状态标签 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让“展开完整回复”在浅色工作台中成为清晰的主操作，并从会话卡片中移除执行状态标签。

**Architecture:** 移除 `SessionRail.vue` 的会话执行状态分支及其专用样式；在既有样式表中把展开回复控件提升为主按钮，并以已有 `aria-expanded` 属性表达收起状态。Vitest 源码契约测试覆盖这两个界面约束，不触及 API、状态管理或运行活动栏。

**Tech Stack:** Vue 3、TypeScript、CSS、Vitest、Vite。

## Global Constraints

- 仅使用 `--wb-accent`、`--accent-strong`、`--wb-line` 与 `--wb-text`，不新增设计令牌。
- 保留 `ChatCanvas.vue` 的 `aria-expanded`、文案与 `toggleExpanded()` 行为。
- 移除会话卡片状态标签，保留顶部 `RunActivityBar.vue` 的实时状态。
- 改动后必须通过 `npm test`、`npm run build` 和 `npm run verify:web`。

---

### Task 1: 先写失败的界面契约测试

**Files:**

- Modify: `apps/web/tests/frontend-plan.spec.mjs`
- Test: `apps/web/tests/frontend-plan.spec.mjs`

**Interfaces:**

- Consumes: `.message-expand`、`aria-expanded`、`SessionRail.vue` 会话卡片和两个样式表。
- Produces: 主按钮、收起次级状态与无会话标签的回归测试。

- [ ] **Step 1: 在 `frontend plan contracts` 中添加失败用例**

```js
it('makes the collapsed reply action prominent and removes session status badges', async () => {
  const [chat, sessions, workbenchCss, lightTheme] = await Promise.all([
    read('../src/components/ChatCanvas.vue'),
    read('../src/components/SessionRail.vue'),
    read('../src/styles/workbench.css'),
    read('../src/styles/light-theme.css')
  ]);

  expect(chat).toContain('class="message-expand"');
  expect(chat).toContain(':aria-expanded="expanded.has(index)"');
  expect(workbenchCss).toMatch(/\.message-expand\s*\{[\s\S]*min-height:\s*40px;[\s\S]*color:\s*#fff;[\s\S]*background:\s*var\(--wb-accent\);/);
  expect(workbenchCss).toContain(".message-expand[aria-expanded='true']");
  expect(workbenchCss).toContain('.message-expand:hover:not(:disabled)');
  expect(workbenchCss).toContain('.message-expand:active:not(:disabled)');
  expect(sessions).not.toContain('session-state');
  expect(sessions).not.toContain('function statusLabel');
  expect(lightTheme).not.toContain('.session-state');
});
```

- [ ] **Step 2: 运行测试，确认它因未实现而失败**

Run: `npm test -- --run tests/frontend-plan.spec.mjs`

Expected: FAIL；`.message-expand` 缺少 40px 高度、主色背景或展开状态选择器，且会话组件仍包含 `session-state`。

- [ ] **Step 3: 再次运行以确认红灯稳定**

Run: `npm test -- --run tests/frontend-plan.spec.mjs`

Expected: FAIL，失败来源仍是新加入的用例。

- [ ] **Step 4: 提交测试基线**

Run:

```bash
git add apps/web/tests/frontend-plan.spec.mjs
git commit -m "test: specify message and session controls"
```

### Task 2: 实现会话卡片与回复控件

**Files:**

- Modify: `apps/web/src/components/SessionRail.vue:1-104`
- Modify: `apps/web/src/styles/workbench.css:575-592,1079-1097`
- Modify: `apps/web/src/styles/light-theme.css:87-95`
- Test: `apps/web/tests/frontend-plan.spec.mjs`

**Interfaces:**

- Consumes: 会话 API 的 `latest_execution_status`，但不再在 `SessionRail.vue` 中显示。
- Consumes: `ChatCanvas.vue` 现有 `.message-expand[aria-expanded]` 绑定。
- Produces: 主/次级状态清晰的回复按钮和不含状态标签的会话卡片。

- [ ] **Step 1: 删除会话状态转换函数与模板块**

从 `SessionRail.vue` 删除整个 `statusLabel(value: string | null)` 函数及下列模板块：

```vue
<span v-if="statusLabel(session.latest_execution_status)" class="session-state">
  {{ statusLabel(session.latest_execution_status) }}
</span>
```

保留 `.session-title` 与 `.session-meta`，使卡片继续显示标题、消息数量和相对时间。

- [ ] **Step 2: 删除已无消费方的标签样式**

删除 `workbench.css` 中完整的 `.session-state { ... }` 规则，并从 `light-theme.css` 的联合选择器中移除 `.workbench-shell .session-state,`。不改变 `.session-card`、删除按钮或运行活动栏。

- [ ] **Step 3: 用以下规则替换回复按钮样式**

```css
.message-expand {
  display: inline-flex;
  min-height: 40px;
  align-items: center;
  justify-content: center;
  margin: 10px auto 0;
  padding: 0 16px;
  color: #fff;
  background: var(--wb-accent);
  border: 0;
  border-radius: var(--radius-control);
  font-size: 12px;
  font-weight: 600;
  transition: background-color 180ms ease, transform 180ms ease;
}

.message-expand:hover:not(:disabled) {
  background: var(--accent-strong);
  transform: translateY(-1px);
}

.message-expand:active:not(:disabled) {
  transform: translateY(1px);
}

.message-expand[aria-expanded='true'] {
  color: var(--wb-text);
  background: rgb(255 255 252 / 0.5);
  border: 1px solid var(--wb-line);
}
```

无需额外焦点规则：全局 `.workbench-shell :where(button, a, input, textarea):focus-visible` 已提供可见焦点环。

- [ ] **Step 4: 运行目标测试，确认绿色通过**

Run: `npm test -- --run tests/frontend-plan.spec.mjs`

Expected: PASS，新增界面契约与现有契约全部通过。

- [ ] **Step 5: 提交实现**

Run:

```bash
git add apps/web/src/components/SessionRail.vue apps/web/src/styles/workbench.css apps/web/src/styles/light-theme.css apps/web/tests/frontend-plan.spec.mjs
git commit -m "fix: emphasize reply expansion and simplify sessions"
```

### Task 3: 验证交付

**Files:**

- Verify: `apps/web/src/components/ChatCanvas.vue`
- Verify: `apps/web/src/components/SessionRail.vue`
- Verify: `apps/web/src/styles/workbench.css`
- Verify: `apps/web/src/styles/light-theme.css`

**Interfaces:**

- Consumes: Task 2 的组件和样式。
- Produces: 可复查的前端验证结果。

- [ ] **Step 1: 执行完整前端测试**

Run: `npm test`

Expected: PASS，所有 Vitest 用例通过。

- [ ] **Step 2: 执行生产构建**

Run: `npm run build`

Expected: EXIT 0，`vue-tsc -b` 和 Vite 打包完成。

- [ ] **Step 3: 执行项目 Web 校验**

Run: `npm run verify:web`

Expected: EXIT 0，项目内前端校验完成。

- [ ] **Step 4: 检查关键交互**

启动已有 Web 开发环境并检查：折叠长回复显示深绿色“展开完整回复”；点击后显示浅色描边“收起回复”；会话卡片只显示标题和元信息；键盘 Tab 聚焦时按钮有全局可见焦点环。

- [ ] **Step 5: 提交已验证的改动**

Run:

```bash
git status --short
git add apps/web/src/components/SessionRail.vue apps/web/src/styles/workbench.css apps/web/src/styles/light-theme.css apps/web/tests/frontend-plan.spec.mjs
git commit -m "fix: verify reply and session interface"
```
