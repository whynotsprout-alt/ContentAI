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

  it('keeps tool activity transient instead of defining workflow stages', async () => {
    const [app, activity] = await Promise.all([
      read('../src/App.vue'),
      read('../src/components/RunActivityBar.vue')
    ]);
    expect(app).not.toMatch(/pinnedSessionIds|archivedSessionIds|workflowStages/);
    expect(activity).toContain("tool_start");
    expect(activity).not.toMatch(/research_package|draft_version|workflow_stage/);
  });

  it('keeps the primary status copy but hides activity details', async () => {
    const activity = await read('../src/components/RunActivityBar.vue');
    expect(activity).toContain('progressLabel');
    expect(activity).not.toContain('activity-detail');
    expect(activity).not.toContain('source-health');
    expect(activity).not.toContain('source_health');
    expect(activity).not.toContain('candidate_count');
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
      read('../src/styles/light-theme.css')
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
