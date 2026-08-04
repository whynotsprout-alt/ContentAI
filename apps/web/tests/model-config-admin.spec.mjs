import { createRenderer, nextTick, ssrContextKey } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

vi.mock('../src/features/admin/components/AdminShell.vue', async () => {
  const { defineComponent, h } = await import('vue');
  return {
    default: defineComponent({
      name: 'AdminShellStub',
      setup(_props, { slots }) {
        return () => h('div', [slots['heading-status']?.(), slots.default?.()]);
      }
    })
  };
});

const hostNode = (type, text = '') => ({ type, text, props: {}, children: [], parent: null });
const renderer = createRenderer({
  patchProp(node, key, _previous, value) {
    node.props[key] = value;
  },
  insert(child, parent, anchor = null) {
    child.parent = parent;
    const anchorIndex = anchor ? parent.children.indexOf(anchor) : -1;
    if (anchorIndex >= 0) parent.children.splice(anchorIndex, 0, child);
    else parent.children.push(child);
  },
  remove(child) {
    if (!child.parent) return;
    const index = child.parent.children.indexOf(child);
    if (index >= 0) child.parent.children.splice(index, 1);
    child.parent = null;
  },
  createElement(type) {
    return hostNode(type);
  },
  createText(text) {
    return hostNode('text', text);
  },
  createComment(text) {
    return hostNode('comment', text);
  },
  setText(node, text) {
    node.text = text;
  },
  setElementText(node, text) {
    node.text = text;
    node.children = [];
  },
  parentNode(node) {
    return node.parent;
  },
  nextSibling(node) {
    if (!node.parent) return null;
    const index = node.parent.children.indexOf(node);
    return node.parent.children[index + 1] ?? null;
  },
  setScopeId(node, id) {
    node.props[id] = '';
  },
  insertStaticContent(content, parent, anchor) {
    const node = hostNode('static', content);
    this.insert(node, parent, anchor);
    return [node, node];
  }
});

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

const flushComponent = async () => {
  await Promise.resolve();
  await Promise.resolve();
  await nextTick();
};

const configuredModel = (overrides = {}) => ({
  configured: true,
  id: 'model-config-v1',
  version: 1,
  provider: 'openai_compatible',
  base_url: 'https://models.example.test/v1',
  model_name: 'model-v1',
  api_key_hint: 'key-…v1',
  input_price_per_million_usd: 5,
  output_price_per_million_usd: 25,
  temperature: 0.2,
  context_window_tokens: 32_000,
  chat_max_tokens: 8_000,
  structured_max_tokens: 8_000,
  validated_at: '2026-07-22T00:00:00Z',
  created_at: '2026-07-22T00:00:00Z',
  created_by_user_id: 'admin-v1',
  created_by_email: 'admin@example.test',
  ...overrides
});

async function mountAdminModels() {
  const { default: AdminModelsView } = await import('../src/features/admin/views/AdminModelsView.vue');
  const container = hostNode('root');
  const app = renderer.createApp({ ...AdminModelsView, render: () => null });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount(container);
  await flushComponent();
  return { app, state: app._instance.setupState };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('admin model configuration', () => {
  it('declares runtime and pricing fields while keeping probe payload connection-only', async () => {
    const [api, guard] = await Promise.all([
      read('../src/shared/services/api.ts'),
      read('../src/features/admin/modelConfigRequestGuard.ts')
    ]);
    const configuration = api.slice(
      api.indexOf('interface AdminModelConfigurationMetadata'),
      api.indexOf('export interface AdminModelProbeResult')
    );
    const probePayload = api.slice(
      api.indexOf('export interface AdminModelProbePayload'),
      api.indexOf('export interface AdminModelUpdatePayload')
    );
    const updatePayload = api.slice(
      api.indexOf('export interface AdminModelUpdatePayload'),
      api.indexOf('export const authApi')
    );

    expect(configuration).toContain('configured: true;');
    expect(configuration).toContain('configured: false;');
    expect(configuration).toMatch(/\n\s*temperature: number \| null;/);
    expect(updatePayload).toMatch(/\n\s*temperature: number \| null;/);
    for (const field of ['context_window_tokens', 'chat_max_tokens', 'structured_max_tokens']) {
      expect(configuration).toMatch(new RegExp(`\\n\\s*${field}: number;`));
      expect(updatePayload).toMatch(new RegExp(`\\n\\s*${field}: number;`));
      expect(probePayload).not.toContain(field);
    }
    for (const field of ['input_price_per_million_usd', 'output_price_per_million_usd']) {
      expect(configuration).toContain(`${field}?: number | null;`);
      expect(updatePayload).toContain(`${field}: number;`);
      expect(probePayload).not.toContain(field);
    }
    for (const field of ['temperature', 'contextWindowTokens', 'chatMaxTokens', 'structuredMaxTokens']) {
      expect(guard).toMatch(new RegExp(`${field}\\?: number \\| null;`));
    }
    expect(guard).toContain('inputPrice?: string;');
    expect(guard).toContain('outputPrice?: string;');
  });

  it('keeps runtime and prices in save signatures but outside connection probe signatures', async () => {
    const { createModelConfigRequestGuard } = await import('../src/features/admin/modelConfigRequestGuard.ts');
    let draft = {
      baseUrl: 'https://models.example.test/v1',
      apiKey: '',
      modelName: 'model-v1',
      inputPrice: '5',
      outputPrice: '25',
      temperature: 0.2,
      contextWindowTokens: 32_000,
      chatMaxTokens: 8_000,
      structuredMaxTokens: 8_000
    };
    const probeGuard = createModelConfigRequestGuard(() => ({
      baseUrl: draft.baseUrl,
      apiKey: draft.apiKey,
      modelName: draft.modelName
    }));
    const saveGuard = createModelConfigRequestGuard(() => draft);
    const probeTicket = probeGuard.begin();
    const saveTicket = saveGuard.begin();

    draft = { ...draft, temperature: null, inputPrice: '6' };

    expect(probeGuard.isCurrent(probeTicket)).toBe(true);
    expect(saveGuard.isCurrent(saveTicket)).toBe(false);
  });

  it('uses suggested runtime and Claude Opus pricing defaults when unconfigured', async () => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue({
      configured: false,
      input_price_per_million_usd: null,
      output_price_per_million_usd: null,
      temperature: null,
      context_window_tokens: null,
      chat_max_tokens: null,
      structured_max_tokens: null
    });
    const { app, state } = await mountAdminModels();

    try {
      expect(state.temperature).toBe(0.2);
      expect(state.temperatureMode).toBe('auto');
      expect(state.selectedTemperature).toBeNull();
      expect(state.contextWindowTokens).toBe(32_000);
      expect(state.chatMaxTokens).toBe(8_000);
      expect(state.structuredMaxTokens).toBe(8_000);
      expect(state.inputPrice).toBe('5');
      expect(state.outputPrice).toBe('25');
    } finally {
      app.unmount();
    }
  });

  it('keeps safe defaults when an older configured response omits runtime fields', async () => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue({
      configured: true,
      id: 'legacy-model-config',
      version: 3,
      base_url: 'https://models.example.test/v1',
      model_name: 'legacy-model',
      api_key_hint: 'key-…old',
      input_price_per_million_usd: 5,
      output_price_per_million_usd: 25
    });
    const { app, state } = await mountAdminModels();

    try {
      expect(state.temperatureMode).toBe('auto');
      expect(state.contextWindowTokens).toBe(32_000);
      expect(state.chatMaxTokens).toBe(8_000);
      expect(state.structuredMaxTokens).toBe(8_000);
      expect(state.canSave).toBe(true);
    } finally {
      app.unmount();
    }
  });

  it('submits automatic temperature and the complete runtime and pricing snapshot', async () => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue(configuredModel({ temperature: null }));
    const update = vi.spyOn(adminApi, 'updateModelConfig').mockResolvedValue(configuredModel({
      version: 2,
      temperature: null,
      context_window_tokens: 200_000,
      chat_max_tokens: 12_000,
      structured_max_tokens: 6_000,
      input_price_per_million_usd: 6,
      output_price_per_million_usd: 30
    }));
    const { app, state } = await mountAdminModels();

    try {
      state.contextWindowTokens = 200_000;
      state.chatMaxTokens = 12_000;
      state.structuredMaxTokens = 6_000;
      state.inputPrice = '6';
      state.outputPrice = '30';
      await nextTick();
      await state.saveConfiguration();

      expect(update).toHaveBeenCalledWith({
        base_url: 'https://models.example.test/v1',
        model_name: 'model-v1',
        input_price_per_million_usd: 6,
        output_price_per_million_usd: 30,
        expected_version: 1,
        temperature: null,
        context_window_tokens: 200_000,
        chat_max_tokens: 12_000,
        structured_max_tokens: 6_000
      });
      expect(state.active.version).toBe(2);
      expect(state.temperatureMode).toBe('auto');
    } finally {
      app.unmount();
    }
  });

  it('restores the last custom temperature after switching through auto mode', async () => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue(configuredModel({ temperature: 0.7 }));
    const { app, state } = await mountAdminModels();

    try {
      expect(state.temperatureMode).toBe('custom');
      state.setTemperatureMode('auto');
      expect(state.selectedTemperature).toBeNull();
      state.setTemperatureMode('custom');
      expect(state.selectedTemperature).toBe(0.7);
    } finally {
      app.unmount();
    }
  });

  it.each([
    ['temperature below zero', 'temperature', -0.01],
    ['temperature above two', 'temperature', 2.01],
    ['temperature NaN', 'temperature', Number.NaN],
    ['context zero', 'contextWindowTokens', 0],
    ['context fractional', 'contextWindowTokens', 32_000.5],
    ['chat zero', 'chatMaxTokens', 0],
    ['chat equal to context', 'chatMaxTokens', 32_000],
    ['structured zero', 'structuredMaxTokens', 0],
    ['structured above context', 'structuredMaxTokens', 32_001]
  ])('disables save for invalid runtime boundary: %s', async (_label, field, value) => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue(configuredModel());
    const { app, state } = await mountAdminModels();

    try {
      state[field] = value;
      await nextTick();
      expect(state.canSave).toBe(false);
    } finally {
      app.unmount();
    }
  });

  it.each([0, 2])('accepts inclusive custom temperature boundary %s', async (value) => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue(configuredModel());
    const { app, state } = await mountAdminModels();

    try {
      state.temperature = value;
      await nextTick();
      expect(state.canSave).toBe(true);
    } finally {
      app.unmount();
    }
  });

  it('lets a connection probe finish when only runtime parameters and prices change', async () => {
    const { adminApi } = await import('../src/shared/services/api.ts');
    const probeRequest = deferred();
    vi.spyOn(adminApi, 'modelConfig').mockResolvedValue(configuredModel());
    vi.spyOn(adminApi, 'probeModelConfig').mockReturnValue(probeRequest.promise);
    const { app, state } = await mountAdminModels();

    try {
      const pendingProbe = state.runProbe(false);
      state.temperature = 0.7;
      state.contextWindowTokens = 64_000;
      state.inputPrice = '6';
      await nextTick();
      probeRequest.resolve({
        base_url: 'https://models.example.test/v1',
        models: ['model-v1', 'model-v2'],
        models_truncated: false,
        model_validated: false,
        latency_ms: 24
      });
      await pendingProbe;
      await nextTick();

      expect(state.models).toEqual(['model-v1', 'model-v2']);
      expect(state.latencyMs).toBe(24);
      expect(state.successMessage).toBe('已刷新 2 个可用模型。');
    } finally {
      app.unmount();
    }
  });

  it('accepts only the newest response for the current normalized draft', async () => {
    const { createModelConfigRequestGuard } = await import('../src/features/admin/modelConfigRequestGuard.ts');
    let draft = { baseUrl: ' https://a.example/v1/ ', apiKey: 'key-a', modelName: ' model-a ' };
    const guard = createModelConfigRequestGuard(() => draft);
    const applied = [];
    let resolveA;
    let resolveB;
    const responseA = new Promise((resolve) => { resolveA = resolve; });
    const responseB = new Promise((resolve) => { resolveB = resolve; });

    const run = async (response) => {
      const ticket = guard.begin();
      const value = await response;
      if (guard.isCurrent(ticket)) applied.push(value);
    };

    const pendingA = run(responseA);
    draft = { baseUrl: 'https://b.example/v1', apiKey: 'key-b', modelName: 'model-b' };
    guard.invalidate();
    const pendingB = run(responseB);
    resolveB('new-response');
    await pendingB;
    resolveA('stale-response');
    await pendingA;

    expect(applied).toEqual(['new-response']);
  });

  it('registers an admin-only models route and shared admin shell', async () => {
    const [router, users, models] = await Promise.all([
      read('../src/app/router.ts'),
      read('../src/features/admin/views/AdminUsersView.vue'),
      read('../src/features/admin/views/AdminModelsView.vue')
    ]);

    expect(router).toContain("path: '/admin/models'");
    expect(router).toContain("name: 'admin-models'");
    expect(router).toContain("meta: { admin: true");
    expect(users).toContain("import AdminShell from '@/features/admin/components/AdminShell.vue'");
    expect(models).toContain("import AdminShell from '@/features/admin/components/AdminShell.vue'");
  });

  it('never exposes a saved key and keeps blank key semantics explicit', async () => {
    const [api, models] = await Promise.all([
      read('../src/shared/services/api.ts'),
      read('../src/features/admin/views/AdminModelsView.vue')
    ]);

    expect(api).toContain('api_key_hint?: string | null');
    expect(api).not.toMatch(/api_key_ciphertext/);
    expect(models).toContain('留空表示不更换已保存的 Key');
    expect(models).toContain('type="password"');
    expect(models).not.toMatch(/v-model="[^\"]*api_key_hint/);
  });

  it('supports refresh, test, custom model, save, conflict, and live feedback states', async () => {
    const models = await read('../src/features/admin/views/AdminModelsView.vue');

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
    expect(models).toContain('自动（由模型决定）');
    expect(models).toContain('id="model-temperature-mode"');
    expect(models).toContain('id="model-context-window"');
    expect(models).toContain('id="model-chat-max"');
    expect(models).toContain('id="model-structured-max"');
    expect(models).toContain('class="model-runtime-summary"');
    expect(models).toContain('probeGuard.isCurrent');
    expect(models).toContain('saveGuard.isCurrent');
    expect(models).toContain('input_price_per_million_usd');
    expect(models).toContain('output_price_per_million_usd');
    expect(models).toContain('Claude Opus 4.8');
  });

  it('clears only stale probe feedback and preserves draft while every conflict refreshes the active version', async () => {
    const models = await read('../src/features/admin/views/AdminModelsView.vue');

    expect(models).toContain("MODEL_CONFIG_PERSISTENCE_FAILED: '模型配置保存失败，请稍后重试。'");
    expect(models).toContain("const errorKind = ref<'load' | 'probe' | 'save' | ''>('');");
    expect(models).toContain("if (errorKind.value === 'probe') {");
    expect(models).toContain("errorKind.value = 'probe';");
    expect(models).toMatch(/if \(error instanceof ApiError && error\.code === 'MODEL_CONFIG_CHANGED'\) \{\s*try \{\s*await loadConfiguration\(\{ preserveForm: true, background: true, propagate: true \}\);/);
    expect(models).toContain('if (saveGuard.isCurrent(request)) {');
    expect(models).toContain('saving.value = false;');
    expect(models).not.toContain('if (saveGuard.isLatest(request)) saving.value = false;');
  });

  it.each(['success', 'conflict', 'failure'])(
    'releases save ownership after an edited draft settles with %s',
    async (outcome) => {
      const { adminApi, ApiError } = await import('../src/shared/services/api.ts');
      const initial = configuredModel();
      const refreshed = {
        ...initial,
        id: 'model-config-v2',
        version: 2,
        model_name: 'model-v2'
      };
      const saveRequest = deferred();
      const load = vi.spyOn(adminApi, 'modelConfig').mockResolvedValueOnce(initial);
      if (outcome === 'conflict') load.mockResolvedValueOnce(refreshed);
      vi.spyOn(adminApi, 'updateModelConfig').mockReturnValue(saveRequest.promise);
      const { app, state } = await mountAdminModels();

      try {
        expect(state.loading).toBe(false);
        expect(state.canSave).toBe(true);
        const pendingSave = state.saveConfiguration();
        expect(state.saving).toBe(true);

        const editedBaseUrl = 'https://edited.example.test/v1';
        state.baseUrl = editedBaseUrl;
        await nextTick();

        if (outcome === 'success') {
          saveRequest.resolve(refreshed);
        } else if (outcome === 'conflict') {
          saveRequest.reject(new ApiError(409, {
            code: 'MODEL_CONFIG_CHANGED',
            message: 'configuration changed',
            retryable: false
          }));
        } else {
          saveRequest.reject(new Error('save failed'));
        }
        await pendingSave;
        await nextTick();

        expect(state.saving).toBe(false);
        expect(state.baseUrl).toBe(editedBaseUrl);
        expect(state.modelName).toBe('model-v1');
        expect(state.canSave).toBe(true);
        if (outcome === 'conflict') {
          expect(load).toHaveBeenCalledTimes(2);
          expect(state.active.version).toBe(2);
        } else {
          expect(state.active.version).toBe(1);
        }
      } finally {
        app.unmount();
      }
    }
  );

  it('provides a keyboard skip target and route focus management in the shared shell', async () => {
    const [shell, styles] = await Promise.all([
      read('../src/features/admin/components/AdminShell.vue'),
      read('../src/shared/styles/base.css')
    ]);

    expect(shell).toContain('href="#admin-main-content"');
    expect(shell).toContain('id="admin-main-content"');
    expect(shell).toContain('tabindex="-1"');
    expect(shell).toContain('preventScroll: true');
    expect(styles).toContain('.skip-link:focus');
  });

  it('keeps the shared users page aligned with cursor and audit APIs', async () => {
    const [api, users] = await Promise.all([
      read('../src/shared/services/api.ts'),
      read('../src/features/admin/views/AdminUsersView.vue')
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

  it('keeps all, chat, and background token usage visually distinct', async () => {
    const [api, users] = await Promise.all([
      read('../src/shared/services/api.ts'),
      read('../src/features/admin/views/AdminUsersView.vue')
    ]);

    for (const field of [
      'chat_input_tokens',
      'chat_output_tokens',
      'chat_total_tokens',
      'background_input_tokens',
      'background_output_tokens',
      'background_total_tokens',
      'input_cost_usd',
      'output_cost_usd',
      'total_cost_usd',
      'chat_total_cost_usd',
      'background_total_cost_usd',
      'completed_usage_call_count',
      'failed_usage_call_count'
    ]) {
      expect(api).toContain(`${field}: number`);
      expect(users).toContain(`selected.${field}`);
    }
    expect(users).toContain('全部 Token');
    expect(users).toContain('全部模型');
    expect(users).toContain('后台处理');
    expect(users).toContain('成功调用');
    expect(users).toContain('缺失');
    expect(users).toContain('失败');
  });

  it('maps unconfigured chat submissions without retaining optimistic messages or retrying', async () => {
    const store = await read('../src/features/workbench/stores/workbench.store.ts');

    expect(store).toContain("error.code === 'MODEL_NOT_CONFIGURED'");
    expect(store).toContain('模型服务尚未配置，请联系管理员');
    expect(store).toContain('this.messages.splice(optimisticIndex, 1)');
    expect(store).not.toMatch(/MODEL_NOT_CONFIGURED[\s\S]{0,300}(retry|setTimeout|listen\()/);
  });
});
