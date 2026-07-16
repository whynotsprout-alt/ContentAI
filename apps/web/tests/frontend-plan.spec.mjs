import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

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

  it('shows hotspot source health and candidate-pool contribution', async () => {
    const activity = await read('../src/components/RunActivityBar.vue');
    expect(activity).toContain('source_health');
    expect(activity).toContain('item_count');
    expect(activity).toContain('selected_count');
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
    const dialog = await read('../src/components/AccessibleDialog.vue');
    expect(dialog).toContain("event.key === 'Escape'");
    expect(dialog).toContain("event.key !== 'Tab'");
    expect(dialog).toContain('child.inert = true');
    expect(dialog).toContain('previousFocus?.focus()');
  });
});
