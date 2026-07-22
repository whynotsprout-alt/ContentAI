import { createRenderer, nextTick, ssrContextKey } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

vi.mock('../src/components/AdminShell.vue', async () => {
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

async function mountAdminModels() {
  const { default: AdminModelsView } = await import('../src/views/AdminModelsView.vue');
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
  it('accepts only the newest response for the current normalized draft', async () => {
    const { createModelConfigRequestGuard } = await import('../src/views/modelConfigRequestGuard.ts');
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
    expect(models).toContain('probeGuard.isCurrent');
    expect(models).toContain('saveGuard.isCurrent');
  });

  it('clears only stale probe feedback and preserves draft while every conflict refreshes the active version', async () => {
    const models = await read('../src/views/AdminModelsView.vue');

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
      const { adminApi, ApiError } = await import('../src/services/api.ts');
      const initial = {
        configured: true,
        id: 'model-config-v1',
        version: 1,
        base_url: 'https://models.example.test/v1',
        model_name: 'model-v1',
        api_key_hint: 'key-hint-v1',
        validated_at: '2026-07-22T00:00:00Z'
      };
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
        }
      } finally {
        app.unmount();
      }
    }
  );

  it('provides a keyboard skip target and route focus management in the shared shell', async () => {
    const [shell, styles] = await Promise.all([
      read('../src/components/AdminShell.vue'),
      read('../src/styles/base.css')
    ]);

    expect(shell).toContain('href="#admin-main-content"');
    expect(shell).toContain('id="admin-main-content"');
    expect(shell).toContain('tabindex="-1"');
    expect(shell).toContain('preventScroll: true');
    expect(styles).toContain('.skip-link:focus');
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
