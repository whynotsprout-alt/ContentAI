import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

describe('admin model configuration', () => {
  it('registers an admin-only models route and shared admin shell', async () => {
    const [router, users, models] = await Promise.all([
      read('../src/router.ts'),
      read('../src/views/AdminUsersView.vue'),
      read('../src/views/AdminModelsView.vue')
    ]);

    expect(router).toContain("path: '/admin/models'");
    expect(router).toContain("name: 'admin-models'");
    expect(router).toContain("meta: { admin: true");
    expect(users).toContain("import AdminShell from '../components/AdminShell.vue'");
    expect(models).toContain("import AdminShell from '../components/AdminShell.vue'");
  });

  it('never exposes a saved key and keeps blank key semantics explicit', async () => {
    const [api, models] = await Promise.all([
      read('../src/services/api.ts'),
      read('../src/views/AdminModelsView.vue')
    ]);

    expect(api).toContain('api_key_hint?: string | null');
    expect(api).not.toMatch(/api_key_ciphertext/);
    expect(models).toContain('留空表示不更换已保存的 Key');
    expect(models).toContain('type="password"');
    expect(models).not.toMatch(/v-model="[^\"]*api_key_hint/);
  });

  it('supports refresh, test, custom model, save, conflict, and live feedback states', async () => {
    const models = await read('../src/views/AdminModelsView.vue');

    expect(models).toContain('adminApi.probeModelConfig');
    expect(models).toContain('adminApi.updateModelConfig');
    expect(models).toContain('刷新模型');
    expect(models).toContain('测试连接');
    expect(models).toContain('保存并启用');
    expect(models).toContain('可选择候选，也可直接输入自定义模型 ID');
    expect(models).toContain("error.code === 'MODEL_CONFIG_CHANGED'");
    expect(models).toContain('initialLoadFailed');
    expect(models).toContain('versionSyncFailed');
    expect(models).toContain('preserveForm: true');
    expect(models).toContain('model_validated');
    expect(models).toContain('aria-live="polite"');
    expect(models).toContain(':aria-busy="loading || probing || saving"');
  });

  it('keeps the shared users page aligned with cursor and audit APIs', async () => {
    const [api, users] = await Promise.all([
      read('../src/services/api.ts'),
      read('../src/views/AdminUsersView.vue')
    ]);

    expect(api).toContain("query.set('cursor', params.cursor)");
    expect(api).toContain("query.set('limit', String(params.limit ?? 50))");
    expect(api).toContain('/temporary-password');
    expect(api).toContain('/messages?');
    expect(api).not.toContain('/password-reset');
    expect(users).toContain('adminApi.sessionMessages');
    expect(users).toContain('temporaryPassword');
    expect(users).toContain('sessionsNextCursor');
    expect(users).toContain('messagesNextCursor');
    expect(users).toContain('loadMoreSessions');
    expect(users).toContain('loadMoreMessages');
    expect(users).not.toContain('page_size: pageSize');
  });

  it('maps unconfigured chat submissions without retaining optimistic messages or retrying', async () => {
    const store = await read('../src/stores/workbench.ts');

    expect(store).toContain("error.code === 'MODEL_NOT_CONFIGURED'");
    expect(store).toContain('模型服务尚未配置，请联系管理员');
    expect(store).toContain('this.messages.splice(optimisticIndex, 1)');
    expect(store).not.toMatch(/MODEL_NOT_CONFIGURED[\s\S]{0,300}(retry|setTimeout|listen\()/);
  });
});
