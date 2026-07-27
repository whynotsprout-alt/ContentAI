import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

describe('responsive administration workspace', () => {
  it('keeps every admin destination and utility action available in a safe-area shell', async () => {
    const [shell, styles] = await Promise.all([
      read('../src/components/AdminShell.vue'),
      read('../src/styles/admin.css')
    ]);

    expect(shell).toContain('class="admin-nav-label"');
    expect(shell).toContain('aria-label="返回工作台"');
    expect(shell).toContain('aria-label="退出管理后台"');
    expect(styles).toContain('@media (max-width: 1279px)');
    expect(styles).toContain('env(safe-area-inset-top)');
    expect(styles).toContain('env(safe-area-inset-bottom)');
    expect(styles).toContain('height: 100dvh');
    expect(styles).toContain('var(--product-surface)');
  });

  it('uses explicit compact list and detail states with predictable focus return', async () => {
    const users = await read('../src/views/AdminUsersView.vue');

    expect(users).toContain('const userDetailOpen = ref(false)');
    expect(users).toContain('ref="detailBackButton"');
    expect(users).toContain('class="admin-compact-back"');
    expect(users).toContain('returnToUserList');
    expect(users).toContain('focusUserListTarget');
    expect(users).toContain("'admin-show-detail': userDetailOpen");
  });

  it('shows a temporary password only in a dismiss-and-clear accessible dialog', async () => {
    const users = await read('../src/views/AdminUsersView.vue');

    expect(users).toContain('title-id="temporary-password-title"');
    expect(users).toContain('@close="closeTemporaryPassword"');
    expect(users).toContain('navigator.clipboard.writeText');
    expect(users).toContain('copyTemporaryPassword');
    expect(users).toContain('copyFeedback');
    expect(users).not.toContain('v-if="temporaryPassword" class="admin-temporary-password" role="status"');
  });

  it('preserves audit scroll and selection while switching between index and transcript', async () => {
    const [users, styles] = await Promise.all([
      read('../src/views/AdminUsersView.vue'),
      read('../src/styles/admin.css')
    ]);

    expect(users).toContain('const auditDetailOpen = ref(false)');
    expect(users).toContain('const sessionListScrollTop = ref(0)');
    expect(users).toContain('ref="sessionListPanel"');
    expect(users).toContain('returnToAuditList');
    expect(users).toContain("'audit-show-transcript': auditDetailOpen");
    expect(styles).toContain('overflow-wrap: anywhere');
  });

  it('orders compact model content as summary, form, then expandable full status', async () => {
    const [models, styles] = await Promise.all([
      read('../src/views/AdminModelsView.vue'),
      read('../src/styles/admin.css')
    ]);
    const summary = models.indexOf('class="model-status-summary"');
    const form = models.indexOf('class="model-config-panel"');
    const details = models.indexOf('class="model-status-details"');

    expect(summary).toBeGreaterThan(-1);
    expect(form).toBeGreaterThan(summary);
    expect(details).toBeGreaterThan(form);
    expect(models).toContain('<summary>');
    expect(styles).toContain('.model-status-summary');
    expect(styles).toContain('.model-status-details');
    expect(styles).toContain('@media (min-width: 1280px)');
  });

  it('uses a semantic accessible runtime fieldset with stable help and error relationships', async () => {
    const models = await read('../src/views/AdminModelsView.vue');

    expect(models).toMatch(/<fieldset[^>]*class="model-runtime-fieldset"[^>]*>/);
    expect(models).toContain('<legend>运行参数</legend>');
    for (const field of ['temperature', 'context-window', 'chat-max', 'structured-max']) {
      expect(models).toContain(`id="model-${field}"`);
      expect(models).toContain(`for="model-${field}"`);
      expect(models).toContain(`id="model-${field}-help"`);
      expect(models).toContain(`id="model-${field}-error"`);
    }
    expect(models.match(/v-model\.number=/g)).toHaveLength(4);
    expect(models.match(/:aria-invalid=/g)).toHaveLength(4);
    expect(models.match(/:aria-describedby=/g)).toHaveLength(4);
    expect(models).toContain('inputmode="decimal"');
    expect(models.match(/inputmode="numeric"/g)).toHaveLength(3);
    expect(models).toContain('min="0"');
    expect(models).toContain('max="2"');
    expect(models).toContain('step="0.1"');
    expect(models).toMatch(/<aside[^>]*class="model-status-panel"[^>]*tabindex="0"[^>]*>/);
  });

  it('adapts runtime fields from two columns to one without changing compact information order', async () => {
    const [models, styles] = await Promise.all([
      read('../src/views/AdminModelsView.vue'),
      read('../src/styles/admin.css')
    ]);

    expect(models).toContain('class="model-runtime-grid"');
    expect(models).toContain('class="model-runtime-summary"');
    expect(styles).toMatch(
      /\.model-runtime-grid\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);[\s\S]*?\}/
    );
    expect(styles).toMatch(
      /@media \(max-width:\s*599px\)\s*\{[\s\S]*?\.model-runtime-grid\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\);/
    );
    expect(styles).toMatch(/\.model-runtime-fieldset\s*\{[\s\S]*?border:\s*0;/);
    expect(styles).toMatch(/\.model-field input,\s*\.model-field select\s*\{[\s\S]*?height:\s*46px;/);
    expect(styles).toContain('overflow-x: hidden');
  });
});
