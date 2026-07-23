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

describe('release cleanup contracts', () => {
  it('deletes retired theme/workbench files and every generic glass hook', async () => {
    const [entry, base, authView, authCss, lightTheme, workbench, adminCss] = await Promise.all([
      read('../src/styles.css'),
      read('../src/styles/base.css'),
      read('../src/views/AuthView.vue'),
      read('../src/styles/auth.css'),
      readOptional('../src/styles/light-theme.css'),
      readOptional('../src/styles/workbench.css'),
      read('../src/styles/admin.css')
    ]);

    expect(entry).not.toContain('light-theme.css');
    expect(lightTheme).toBe('');
    expect(workbench).toBe('');
    expect(`${base}\n${authView}\n${adminCss}`).not.toContain('liquid-glass');
    expect(authView).toContain('class="auth-form-card auth-form-surface"');
    expect(authCss).toContain('.auth-form-surface::before');
  });

  it('keeps one dialog body lock and removes unused backdrop modes and tint', async () => {
    const [base, dialogs, backdrop, backdropCss] = await Promise.all([
      read('../src/styles/base.css'),
      read('../src/styles/dialogs.css'),
      read('../src/components/AmbientBackdrop.vue'),
      read('../src/styles/backdrop.css')
    ]);

    expect(base).not.toContain('body.dialog-open');
    expect(dialogs.match(/body\.dialog-open/g)).toHaveLength(1);
    expect(`${backdrop}\n${backdropCss}`).not.toMatch(/workspace|admin|ambient-backdrop__tint|backdrop-veil/);
  });

  it('removes retired routes, APIs, selectors, and list side stripes without deleting tab indicators', async () => {
    const [router, auth, api, dialogs] = await Promise.all([
      read('../src/router.ts'),
      read('../src/views/AuthView.vue'),
      read('../src/services/api.ts'),
      read('../src/styles/dialogs.css')
    ]);
    const source = `${router}\n${auth}\n${api}\n${dialogs}`;

    expect(source).not.toMatch(/forgot|reset-password|resetPassword|forgotPassword/i);
    expect(dialogs).not.toMatch(/\.agent-directory-row\.active\s*\{[\s\S]*?inset 3px 0/);
    expect(dialogs).toContain('.manager-section-tabs button.active::before');
  });

  it('uses auth-only AA text tokens and 44px minimum targets for known release controls', async () => {
    const [auth, dialogs, admin] = await Promise.all([
      read('../src/styles/auth.css'),
      read('../src/styles/dialogs.css'),
      read('../src/styles/admin.css')
    ]);

    expect(auth).toContain('--auth-muted: #4F5D53');
    expect(auth).toContain('--auth-placeholder: #4B584F');
    expect(auth).toMatch(/\.password-field button[\s\S]*width:\s*44px[\s\S]*height:\s*44px/);
    expect(dialogs).toMatch(/\.manager-sections button[\s\S]*min-height:\s*44px/);
    expect(dialogs).toMatch(/\.source-grid button[\s\S]*min-height:\s*44px/);
    expect(dialogs).toMatch(/\.manager-sections \.manager-back-button[\s\S]*min-width:\s*44px/);
    for (const selector of [
      '.admin-topbar nav button',
      '.admin-primary-nav a',
      '.model-retry-action',
      '.admin-load-more',
      '.admin-pagination button',
      '.admin-detail-alert button',
      '.admin-actions button'
    ]) {
      const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      expect(admin).toMatch(new RegExp(`${escaped}[\\s\\S]*?min-height:\\s*44px`));
    }
  });

  it('keeps build-only tooling in devDependencies and adds axe-core', async () => {
    const pkg = JSON.parse(await read('../package.json'));

    expect(pkg.dependencies).not.toHaveProperty('@vitejs/plugin-vue');
    expect(pkg.devDependencies['@vitejs/plugin-vue']).toBe('^6.0.3');
    expect(pkg.devDependencies['axe-core']).toBeTruthy();
  });
});
