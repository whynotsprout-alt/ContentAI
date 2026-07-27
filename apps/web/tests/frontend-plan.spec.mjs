import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');
const readOptional = async (path) => {
  try {
    return await read(path);
  } catch (error) {
    if (error && typeof error === 'object' && error.code === 'ENOENT') return '';
    throw error;
  }
};

describe('frontend plan contracts', () => {
  it('never submits a placeholder for empty chat input', async () => {
    const source = await read('../src/components/ChatCanvas.vue');
    expect(source).toContain("const value = prompt.value.trim()");
    expect(source).toContain("if (!value)");
    expect(source).not.toContain("defaultPrompt");
  });

  it('removes the global run activity while keeping in-conversation generation feedback', async () => {
    const [app, chat, activity, store] = await Promise.all([
      read('../src/App.vue'),
      read('../src/components/ChatCanvas.vue'),
      readOptional('../src/components/RunActivityBar.vue'),
      read('../src/stores/workbench.ts')
    ]);
    expect(app).not.toContain('RunActivityBar');
    expect(activity).toBe('');
    expect(store).not.toContain("addEventListener('tools'");
    expect(store).not.toMatch(/tool_start|tool_progress|tool_end/);
    expect(chat).toContain('class="assistant-progress"');
    expect(chat).toContain("message.assistant_state === 'pending'");
  });

  it('falls back to three-second run status polling for degraded SSE', async () => {
    const [store, api] = await Promise.all([
      read('../src/stores/workbench.ts'),
      read('../src/services/api.ts')
    ]);
    expect(api).toContain('/api/chat/runs/${runId}/status');
    expect(store).toContain('const RUN_STATUS_POLL_MS = 3000');
    expect(store).toContain("'STREAM_REPLAY_GAP'");
    expect(store).toContain("'STREAM_REPLAY_EXPIRED'");
    expect(store).toContain('api.executionEvents(this.executionId, this.lastEventSequence)');
  });

  it('registers without exposing verification or resend UI', async () => {
    const [auth, router, api, admin] = await Promise.all([
      read('../src/views/AuthView.vue'),
      read('../src/router.ts'),
      read('../src/services/api.ts'),
      read('../src/views/AdminUsersView.vue')
    ]);
    expect(auth).toContain("await router.replace('/login')");
    expect(auth).not.toMatch(/verify-email|registrationSent|resendVerification|resendCooldown/);
    expect(router).not.toContain("path: '/verify-email'");
    expect(api).not.toMatch(/verifyEmail|resendVerification/);
    expect(admin).toContain('canResetPassword');
    expect(admin).toContain('confirmStatusChange');
    expect(admin).not.toMatch(/disabled\s+title=.*暂不/);
  });

  it('traps focus, supports Escape, inerts the page, and restores focus', async () => {
    const [dialog, stack] = await Promise.all([
      read('../src/components/AccessibleDialog.vue'),
      read('../src/components/dialogStack.ts')
    ]);
    expect(dialog).toContain("event.key === 'Escape'");
    expect(dialog).toContain("event.key !== 'Tab'");
    expect(stack).toContain('surface.inert = true');
    expect(dialog).toContain('dialogStack.restoreFocus');
  });

  it('keeps the reply expansion action accessible and removes session status badges', async () => {
    const [chat, sessions, productCss, lightTheme] = await Promise.all([
      read('../src/components/ChatCanvas.vue'),
      read('../src/components/SessionRail.vue'),
      readOptional('../src/styles/product.css'),
      readOptional('../src/styles/light-theme.css')
    ]);

    expect(chat).toContain('class="message-expand"');
    expect(chat).toContain(':aria-expanded="expanded.has(index)"');
    expect(productCss).toMatch(/\.message-expand\s*\{[\s\S]*min-height:\s*44px;/);
    expect(productCss).toContain(".message-expand[aria-expanded='true']");
    expect(productCss).toContain('.message-expand:hover:not(:disabled)');
    expect(productCss).toContain('.message-expand:active:not(:disabled)');
    expect(sessions).not.toContain('session-state');
    expect(sessions).not.toContain('function statusLabel');
    expect(lightTheme).not.toContain('.session-state');
  });
});
