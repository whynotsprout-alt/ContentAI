# Chat Workbench Layout Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a truly centered wide-screen chat reading axis with restrained structural glass surfaces, unchanged assistant readability, and clearer conversational rhythm.

**Architecture:** Keep the existing Vue component tree and responsive Grid/Flex layout. Implement the refresh in the product design tokens and selectors, with contract tests asserting the exact reading measure, safe breakpoint compensation, glass fallback, message surfaces, and preserved responsive behavior. No application state, API, or message-rendering logic changes are required.

**Tech Stack:** Vue 3, TypeScript, Vite, Vitest, CSS custom properties, Playwright-based Web verification, Impeccable detector.

## Global Constraints

- Only modify the authenticated chat workbench; do not change admin, auth, API, store, or execution behavior.
- Use `--conversation-measure: 840px` on desktop and shrink with existing `min()` expressions when space is constrained.
- Enable viewport-center compensation only at `min-width: 1440px`; smaller widths remain centered within the available conversation column.
- Keep the existing `8px / 12px / 16px` control, panel, and overlay radii.
- Apply glass only to `.workbench-header`, `.session-rail`, `.composer`, and existing structural popovers; assistant messages remain transparent and borderless.
- Preserve the mobile drawer, tablet `72px / 280px` rail, desktop `248px / 264px / 280px` rail, 44px targets, focus-visible behavior, safe areas, and reduced motion.
- Provide a near-opaque fallback outside the `backdrop-filter` support block.
- Do not introduce gradients, background video, decorative animation, new dependencies, or duplicated desktop/mobile components.
- Preserve unrelated dirty-worktree changes; stage and commit only files named by each task.

---

## File Map

- Modify `apps/web/tests/workbench-responsive-contract.spec.mjs`: source-level product UI contracts for reading axis, structural glass, message rhythm, and retained responsive behavior.
- Modify `apps/web/src/styles/product.css`: product tokens, shared conversation-axis shift, structural glass fallback/support rules, composer treatment, and message rhythm.
- Do not modify `apps/web/src/components/ChatCanvas.vue`: existing message order and role classes are sufficient for adjacent-message CSS.
- Do not modify `apps/web/src/App.vue`: existing layout inheritance exposes `--session-rail-width` to all chat descendants.

### Task 1: Centered Reading Axis

**Files:**
- Modify: `apps/web/tests/workbench-responsive-contract.spec.mjs:32-53`
- Modify: `apps/web/src/styles/product.css:1-60`
- Modify: `apps/web/src/styles/product.css:676-737`
- Modify: `apps/web/src/styles/product.css:1239-1335`
- Modify: `apps/web/src/styles/product.css:1732-1736`

**Interfaces:**
- Consumes: existing inherited `--session-rail-width` defined on `.workbench-shell`.
- Produces: `--conversation-measure: 840px`, `--conversation-inline-shift`, and one shared selector list that aligns status, messages, approvals, empty state, composer, errors, and hints.

- [ ] **Step 1: Write the failing reading-axis contract**

Replace the current `--conversation-measure: 960px` assertion in the palette test and add a focused test below it:

```js
expect(productCss).toContain('--conversation-measure: 840px');
expect(productCss).toContain('--conversation-inline-shift: 0px');
```

```js
it('centers every conversation surface on the viewport only when the desktop has safe space', async () => {
  const productCss = await readOptional('../src/styles/product.css');
  const desktop1024Start = productCss.indexOf('@media (min-width: 1024px) and (max-width: 1279px)');
  const desktop1280Start = productCss.indexOf('@media (min-width: 1280px) and (max-width: 1439px)');
  const desktop1440Start = productCss.indexOf('@media (min-width: 1440px)');
  const desktop1024Block = productCss.slice(desktop1024Start, desktop1280Start);
  const desktop1280Block = productCss.slice(desktop1280Start, desktop1440Start);

  expect(productCss).toMatch(
    /\.run-activity,[\s\S]*\.workspace-alert,[\s\S]*\.onboarding-empty,[\s\S]*\.message,[\s\S]*\.interrupt-approval,[\s\S]*\.composer-wrap > \.field-error,[\s\S]*\.composer,[\s\S]*\.composer-hint[\s\S]*translate:\s*var\(--conversation-inline-shift\) 0/
  );
  expect(productCss).toMatch(
    /@media \(min-width: 1440px\)[\s\S]*--conversation-inline-shift:\s*calc\(var\(--session-rail-width\) \* -0\.5\)/
  );
  expect(desktop1024Start).toBeGreaterThan(-1);
  expect(desktop1280Start).toBeGreaterThan(desktop1024Start);
  expect(desktop1440Start).toBeGreaterThan(desktop1280Start);
  expect(desktop1024Block).not.toContain('--conversation-inline-shift: calc');
  expect(desktop1280Block).not.toContain('--conversation-inline-shift: calc');
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
Set-Location apps/web
npm.cmd test -- tests/workbench-responsive-contract.spec.mjs
```

Expected: FAIL because the stylesheet still contains `--conversation-measure: 960px`, does not define `--conversation-inline-shift`, and has no shared translated conversation selector.

- [ ] **Step 3: Add the minimal centered-axis implementation**

In the product token block, change and add:

```css
--conversation-measure: 840px;
--conversation-inline-shift: 0px;
```

After the existing `.conversation-column` block, add:

```css
.run-activity,
.workspace-alert,
.onboarding-empty,
.message,
.interrupt-approval,
.composer-wrap > .field-error,
.composer,
.composer-hint {
  translate: var(--conversation-inline-shift) 0;
}
```

Inside the existing `@media (min-width: 1440px)` block, add:

```css
.workbench-shell {
  --session-rail-width: 280px;
  --conversation-inline-shift: calc(var(--session-rail-width) * -0.5);
}
```

Do not add the shift to any smaller media query.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
Set-Location apps/web
npm.cmd test -- tests/workbench-responsive-contract.spec.mjs
```

Expected: PASS for the full `quiet product workbench contract` suite.

- [ ] **Step 5: Commit the centered-axis change**

```powershell
git add -- apps/web/tests/workbench-responsive-contract.spec.mjs apps/web/src/styles/product.css
git commit -m "style(web): center the desktop chat reading axis"
```

### Task 2: Structural Glass and Composer Surface

**Files:**
- Modify: `apps/web/tests/workbench-responsive-contract.spec.mjs`
- Modify: `apps/web/src/styles/product.css:1-60`
- Modify: `apps/web/src/styles/product.css:201-215`
- Modify: `apps/web/src/styles/product.css:362-374`
- Modify: `apps/web/src/styles/product.css:461-483`
- Modify: `apps/web/src/styles/product.css:1239-1274`

**Interfaces:**
- Consumes: current product surface, border, shadow, and focus tokens.
- Produces: `--product-structure-fallback`, `--product-structure-glass`, `--product-glass-blur`, fallback-first structural backgrounds, and a feature-detected blur enhancement.

- [ ] **Step 1: Write the failing structural-glass contract**

Add:

```js
it('uses fallback-first glass only for structural workbench surfaces', async () => {
  const productCss = await readOptional('../src/styles/product.css');

  expect(productCss).toContain('--product-structure-fallback: rgb(255 255 255 / 0.96)');
  expect(productCss).toContain('--product-structure-glass: rgb(255 255 255 / 0.84)');
  expect(productCss).toContain('--product-glass-blur: 18px');
  expect(productCss).toMatch(
    /\.workbench-header[\s\S]*background:\s*var\(--product-structure-fallback\)/
  );
  expect(productCss).toMatch(
    /\.session-rail[\s\S]*background:\s*var\(--product-structure-fallback\)/
  );
  expect(productCss).toMatch(
    /\.composer\s*\{[\s\S]*background:\s*var\(--product-structure-fallback\)/
  );
  expect(productCss).toMatch(
    /\.composer-wrap\s*\{[\s\S]*background:\s*transparent;[\s\S]*border-top:\s*0/
  );
  expect(productCss).toMatch(
    /@supports \(\(backdrop-filter: blur\(1px\)\) or \(-webkit-backdrop-filter: blur\(1px\)\)\)[\s\S]*\.workbench-header,[\s\S]*\.session-rail,[\s\S]*\.composer,[\s\S]*\.popover-menu[\s\S]*backdrop-filter:\s*blur\(var\(--product-glass-blur\)\) saturate\(1\.08\)/
  );
  expect(productCss).toMatch(
    /\.message\.assistant \.message-bubble\s*\{[\s\S]*background:\s*transparent;[\s\S]*border:\s*0/
  );
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
Set-Location apps/web
npm.cmd test -- tests/workbench-responsive-contract.spec.mjs
```

Expected: FAIL because the structural glass tokens and support block do not exist, and `.composer-wrap` is still an opaque bordered footer.

- [ ] **Step 3: Add fallback-first glass tokens and surfaces**

Add to the main product token block:

```css
--product-structure-fallback: rgb(255 255 255 / 0.96);
--product-structure-glass: rgb(255 255 255 / 0.84);
--product-glass-blur: 18px;
```

Change only the structural surface backgrounds:

```css
.workbench-header {
  background: var(--product-structure-fallback);
}

.session-rail {
  background: var(--product-structure-fallback);
}

.popover-menu {
  background: var(--product-structure-fallback);
}

.composer-wrap {
  background: transparent;
  border-top: 0;
}

.composer {
  background: var(--product-structure-fallback);
  box-shadow: 0 4px 8px var(--product-shadow);
}
```

Add one feature-detected enhancement after those base rules:

```css
@supports ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .workbench-header,
  .session-rail,
  .composer,
  .popover-menu {
    background: var(--product-structure-glass);
    backdrop-filter: blur(var(--product-glass-blur)) saturate(1.08);
    -webkit-backdrop-filter: blur(var(--product-glass-blur)) saturate(1.08);
  }
}
```

Do not change the existing rule that keeps authenticated dialogs solid and disables dialog blur.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
Set-Location apps/web
npm.cmd test -- tests/workbench-responsive-contract.spec.mjs
```

Expected: PASS, including the existing “seals authenticated dialogs to solid product surfaces” test.

- [ ] **Step 5: Commit the structural glass change**

```powershell
git add -- apps/web/tests/workbench-responsive-contract.spec.mjs apps/web/src/styles/product.css
git commit -m "style(web): add restrained glass workbench surfaces"
```

### Task 3: Conversational Rhythm and User Surface

**Files:**
- Modify: `apps/web/tests/workbench-responsive-contract.spec.mjs`
- Modify: `apps/web/src/styles/product.css:1-60`
- Modify: `apps/web/src/styles/product.css:831-883`
- Modify: `apps/web/src/styles/product.css:1549-1560`

**Interfaces:**
- Consumes: existing DOM order `.message.user + .message.assistant` and existing role classes from `ChatCanvas.vue`.
- Produces: `--product-user-message`, compact user headers, `24px` user-to-assistant spacing, `48px` completed-turn spacing, and reduced mobile equivalents.

- [ ] **Step 1: Write the failing message-rhythm contract**

Add:

```js
it('keeps assistant prose plain while grouping each user and assistant turn', async () => {
  const productCss = await readOptional('../src/styles/product.css');

  expect(productCss).toContain('--product-user-message: rgb(31 91 69 / 0.08)');
  expect(productCss).toMatch(
    /\.message\s*\{[\s\S]*margin:\s*0 auto var\(--product-space-12\)/
  );
  expect(productCss).toMatch(
    /\.message\.user:has\(\+ \.message\.assistant\)\s*\{[\s\S]*margin-bottom:\s*var\(--product-space-6\)/
  );
  expect(productCss).toMatch(
    /\.message\.user \.message-header\s*\{[\s\S]*min-height:\s*0;[\s\S]*margin-bottom:\s*var\(--product-space-2\)/
  );
  expect(productCss).toMatch(
    /\.message\.user \.message-bubble\s*\{[\s\S]*background:\s*var\(--product-user-message\);[\s\S]*border-radius:\s*var\(--product-radius-panel\)/
  );
  expect(productCss).toMatch(
    /@media \(max-width: 767px\)[\s\S]*\.message\s*\{[\s\S]*margin-bottom:\s*var\(--product-space-8\)[\s\S]*\.message\.user:has\(\+ \.message\.assistant\)[\s\S]*margin-bottom:\s*var\(--product-space-4\)/
  );
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
Set-Location apps/web
npm.cmd test -- tests/workbench-responsive-contract.spec.mjs
```

Expected: FAIL because the user-message token and semantic turn spacing do not exist, and the user header inherits the 44px minimum height.

- [ ] **Step 3: Implement semantic turn spacing**

Add to the product token block:

```css
--product-user-message: rgb(31 91 69 / 0.08);
```

Update the message rules:

```css
.message {
  width: min(var(--conversation-measure), 100%);
  margin: 0 auto var(--product-space-12);
}

.message.user:has(+ .message.assistant) {
  margin-bottom: var(--product-space-6);
}

.message:last-child {
  margin-bottom: var(--product-space-8);
}

.message.user .message-header {
  min-height: 0;
  justify-content: flex-end;
  margin-bottom: var(--product-space-2);
}

.message.user .message-bubble {
  padding: var(--product-space-3) var(--product-space-4);
  background: var(--product-user-message);
  border: 1px solid color-mix(in srgb, var(--product-accent) 16%, transparent);
  border-radius: var(--product-radius-panel);
}
```

Replace the current mobile `.message` override with:

```css
.message {
  margin-bottom: var(--product-space-8);
}

.message.user:has(+ .message.assistant) {
  margin-bottom: var(--product-space-4);
}
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
Set-Location apps/web
npm.cmd test -- tests/workbench-responsive-contract.spec.mjs
```

Expected: PASS for the responsive contract suite.

- [ ] **Step 5: Commit the conversational rhythm change**

```powershell
git add -- apps/web/tests/workbench-responsive-contract.spec.mjs apps/web/src/styles/product.css
git commit -m "style(web): refine chat turn rhythm"
```

### Task 4: Impeccable Polish and Release Verification

**Files:**
- Modify if verification exposes a defect: `apps/web/src/styles/product.css`
- Modify if a contract needs a precise correction: `apps/web/tests/workbench-responsive-contract.spec.mjs`

**Interfaces:**
- Consumes: completed Tasks 1–3.
- Produces: verified desktop, tablet, and mobile layout with zero unresolved Impeccable layout findings.

- [ ] **Step 1: Load the required Impeccable polish workflow**

Read:

```powershell
Get-Content -Raw 'C:\Users\Wna\.agents\skills\impeccable\reference\polish.md'
```

Follow its project/product register checks. Do not rerun `context.mjs`; the session already resolved `docs/PRODUCT.md` and `docs/DESIGN.md`.

- [ ] **Step 2: Run the full Web test suite**

Run:

```powershell
Set-Location apps/web
npm.cmd test
```

Expected: exit code `0`, all Vitest files and tests pass with zero failures.

- [ ] **Step 3: Build the production bundle**

Run:

```powershell
Set-Location apps/web
npm.cmd run build
```

Expected: exit code `0`; `vue-tsc -b` and `vite build` both complete without errors.

- [ ] **Step 4: Run the repository Web verifier**

Run:

```powershell
Set-Location apps/web
npm.cmd run verify:web
```

Expected: exit code `0`, with no responsive, accessibility, or release-gate failure.

- [ ] **Step 5: Inspect the real interface at five widths**

Use the project’s browser verification flow against the running app and capture screenshots at:

```text
2048 × 1024
1440 × 900
1280 × 800
768 × 1024
390 × 844
```

For each viewport, verify:

```text
- 2048 and 1440: message, status, composer, and hint share the viewport-centered axis.
- 1280 and 768: content remains centered inside the available column and never overlaps the rail.
- 390: drawer, safe area, 16px gutter, and 44px send target remain intact.
- All widths: assistant prose is plain; user messages are light green glass-tinted bubbles.
- All widths: no page-level horizontal scrollbar.
- Focus: keyboard focus ring remains visible on header, rail, copy, composer, and send controls.
```

- [ ] **Step 6: Run accessibility inspection**

Run the existing browser accessibility gate or an equivalent Axe pass on the chat workbench.

Expected:

```text
0 critical violations
0 serious violations
No contrast regression on placeholder, muted labels, user bubble, or glass chrome
```

- [ ] **Step 7: Rerun the isolated Impeccable layout scan**

Run:

```powershell
node 'C:\Users\Wna\.agents\skills\impeccable\scripts\detect.mjs' --json --scope layout apps/web/src
```

Expected:

```json
[]
```

- [ ] **Step 8: Run final diff checks**

Run:

```powershell
git diff --check
git diff -- apps/web/src/styles/product.css apps/web/tests/workbench-responsive-contract.spec.mjs
git status --short
```

Expected: no whitespace errors; diff contains only the intended workbench contract and product-style changes plus pre-existing unrelated dirty files.

- [ ] **Step 9: Commit any polish-only correction**

Only if Steps 2–8 required a code correction:

```powershell
git add -- apps/web/src/styles/product.css apps/web/tests/workbench-responsive-contract.spec.mjs
git commit -m "style(web): polish centered chat workspace"
```

If no correction was needed, do not create an empty commit.
