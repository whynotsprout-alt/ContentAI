import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

describe('frontend plan contracts', () => {
  it('never submits a placeholder for empty chat input', async () => {
    const source = await read('../src/features/workbench/components/ChatCanvas.vue');
    expect(source).toContain("const value = prompt.value.trim()");
    expect(source).toContain("if (!value)");
    expect(source).not.toContain("defaultPrompt");
  });

  it('keeps tool activity transient instead of defining workflow stages', async () => {
    const [app, activity] = await Promise.all([
      read('../src/app/App.vue'),
      read('../src/features/workbench/components/RunActivityBar.vue')
    ]);
    expect(app).not.toMatch(/pinnedSessionIds|archivedSessionIds|workflowStages/);
    expect(activity).toContain("tool_start");
    expect(activity).not.toMatch(/research_package|draft_version|workflow_stage/);
  });

  it('keeps the primary status copy but hides activity details', async () => {
    const activity = await read('../src/features/workbench/components/RunActivityBar.vue');
    expect(activity).toContain('progressLabel');
    expect(activity).not.toContain('activity-detail');
    expect(activity).not.toContain('source-health');
    expect(activity).not.toContain('source_health');
    expect(activity).not.toContain('candidate_count');
  });

  it('falls back to three-second run status polling for degraded SSE', async () => {
    const [store, api] = await Promise.all([
      read('../src/features/workbench/stores/workbench.store.ts'),
      read('../src/shared/services/api.ts')
    ]);
    expect(api).toContain('/api/chat/runs/${runId}/status');
    expect(store).toContain('const RUN_STATUS_POLL_MS = 3000');
    expect(store).toContain("'STREAM_REPLAY_GAP'");
    expect(store).toContain("'STREAM_REPLAY_EXPIRED'");
    expect(store).toContain('this._confirmedCursorForExecution(this.executionId)');
  });

  it('avoids full Markdown parsing while an assistant message is streaming', async () => {
    const chat = await read('../src/features/workbench/components/ChatCanvas.vue');
    expect(chat).toContain("message.assistant_state === 'streaming'");
    expect(chat).toContain('markdown.utils.escapeHtml(message.content)');
    expect(chat).toContain('renderedMessageCache');
  });

  it('registers without exposing verification or resend UI', async () => {
    const [auth, router, api, admin] = await Promise.all([
      read('../src/features/auth/AuthView.vue'),
      read('../src/app/router.ts'),
      read('../src/shared/services/api.ts'),
      read('../src/features/admin/views/AdminUsersView.vue')
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
    const dialog = await read('../src/shared/components/AccessibleDialog.vue');
    expect(dialog).toContain("event.key === 'Escape'");
    expect(dialog).toContain("event.key !== 'Tab'");
    expect(dialog).toContain('child.inert = true');
    expect(dialog).toContain('previousFocus?.focus()');
  });

  it('makes the collapsed reply action prominent and removes session status badges', async () => {
    const [chat, sessions, workbenchCss, lightTheme] = await Promise.all([
      read('../src/features/workbench/components/ChatCanvas.vue'),
      read('../src/features/workbench/components/SessionRail.vue'),
      read('../src/shared/styles/workbench.css'),
      read('../src/shared/styles/light-theme.css')
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
});
