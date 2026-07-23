import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

describe('release accessibility and resilience contracts', () => {
  it('associates every Agent editor error and focuses the first invalid control', async () => {
    const manager = await read('../src/components/AgentManager.vue');

    for (const field of ['name', 'positioning', 'scoring', 'content']) {
      expect(manager).toContain(`id="agent-${field}-error"`);
      expect(manager).toContain(`:aria-invalid="touched && Boolean(fieldErrors.${field})"`);
      expect(manager).toContain(`:aria-describedby="touched && fieldErrors.${field} ? 'agent-${field}-error' : undefined"`);
    }
    expect(manager).toContain('id="agent-sources-error"');
    expect(manager).toContain(`aria-describedby="agent-sources-help agent-sources-error"`);
    expect(manager).toContain('async function focusFirstInvalid');
    expect(manager).toContain('await nextTick()');
    expect(manager).toContain('focusFirstInvalid(firstInvalid)');
  });

  it('drops incomplete composite roles in favor of native disclosure buttons', async () => {
    const header = await read('../src/components/WorkbenchHeader.vue');

    expect(header).not.toMatch(/role="(?:listbox|option|menu|menuitem)"/);
    expect(header).not.toMatch(/aria-haspopup="(?:listbox|menu)"/);
    expect(header).not.toContain(':aria-selected');
    expect(header).not.toContain('highlighted');
    expect(header).not.toContain('onAgentKeydown');
    expect(header).toContain('aria-controls="agent-picker-options"');
    expect(header).toContain('id="agent-picker-options"');
    expect(header).toContain('aria-controls="user-account-menu"');
  });

  it('exposes the active session as the current item', async () => {
    const rail = await read('../src/components/SessionRail.vue');

    expect(rail).toContain(`:aria-current="session.session_id === activeSessionId ? 'page' : undefined"`);
  });

  it('uses an h1 for the standalone forced-password page', async () => {
    const [form, view] = await Promise.all([
      read('../src/components/ChangePasswordForm.vue'),
      read('../src/views/ChangePasswordView.vue')
    ]);

    expect(form).toContain(`headingLevel?: 1 | 2`);
    expect(form).toContain(`const headingTag = computed(() => \`h\${props.headingLevel ?? 2}\`)`);
    expect(form).toContain('<component :is="headingTag"');
    expect(view).toContain(':heading-level="1"');
  });

  it('announces clipboard rejection with a recoverable manual-copy instruction', async () => {
    const chat = await read('../src/components/ChatCanvas.vue');

    expect(chat).toMatch(/async function copyMessage[\s\S]*try\s*\{/);
    expect(chat).toMatch(/catch[\s\S]*复制失败，请手动选择回复内容/);
    expect(chat).toContain(`announcement.value = '回复已复制'`);
  });

  it('lets only the latest Agent selection commit state or finish loading', async () => {
    const manager = await read('../src/components/AgentManager.vue');

    expect(manager).toContain('let agentLoadGeneration = 0');
    expect(manager).toContain('const requestGeneration = ++agentLoadGeneration');
    expect(manager).toContain('if (requestGeneration !== agentLoadGeneration) return');
    expect(manager).toContain('if (requestGeneration === agentLoadGeneration) loading.value = false');
  });
});
