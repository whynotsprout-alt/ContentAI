import { createRenderer, nextTick, ssrContextKey } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

const navigation = vi.hoisted(() => ({
  route: { name: 'login', query: {}, fullPath: '/login', meta: { public: true } },
  router: { replace: vi.fn() }
}));
const workbench = vi.hoisted(() => ({
  boot: vi.fn(),
  dispose: vi.fn(),
  interrupt: null
}));

vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal()),
  useRoute: () => navigation.route,
  useRouter: () => navigation.router
}));

vi.mock('../src/features/workbench/stores/workbench.store.ts', () => ({
  useWorkbenchStore: () => workbench
}));

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
  createElement: (type) => hostNode(type),
  createText: (text) => hostNode('text', text),
  createComment: (text) => hostNode('comment', text),
  setText(node, text) {
    node.text = text;
  },
  setElementText(node, text) {
    node.text = text;
    node.children = [];
  },
  parentNode: (node) => node.parent,
  nextSibling(node) {
    if (!node.parent) return null;
    return node.parent.children[node.parent.children.indexOf(node) + 1] ?? null;
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

const temporaryUser = {
  id: 'user-temporary',
  email: 'member@example.test',
  role: 'user',
  status: 'active',
  email_verified_at: '2026-08-01T00:00:00Z',
  password_changed_at: '2026-08-01T00:00:00Z',
  created_at: '2026-08-01T00:00:00Z',
  last_login_at: '2026-08-04T00:00:00Z',
  must_change_password: true,
  temporary_password_expires_at: '2026-08-04T12:00:00Z'
};

const changedUser = {
  ...temporaryUser,
  password_changed_at: '2026-08-04T01:00:00Z',
  must_change_password: false,
  temporary_password_expires_at: null
};

async function mountAuthView(pinia) {
  const { default: AuthView } = await import('../src/features/auth/AuthView.vue');
  setActivePinia(pinia);
  const app = renderer.createApp({ ...AuthView, render: () => null });
  app.use(pinia);
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount(hostNode('root'));
  await nextTick();
  return { app, state: app._instance.setupState };
}

afterEach(() => {
  navigation.router.replace.mockReset();
  workbench.boot.mockReset();
  workbench.dispose.mockReset();
  vi.restoreAllMocks();
});

describe('forced temporary-password flow', () => {
  it('keeps Agent APIs unloaded until password change refreshes the current user', async () => {
    const { api, authApi } = await import('../src/shared/services/api.ts');
    const login = vi.spyOn(authApi, 'login').mockResolvedValue(temporaryUser);
    const changePassword = vi.spyOn(authApi, 'changePassword').mockResolvedValue({ message: '密码已修改' });
    const me = vi.spyOn(authApi, 'me').mockResolvedValue(changedUser);
    const agents = vi.spyOn(api, 'agents');
    const pinia = createPinia();

    navigation.route = { name: 'login', query: { redirect: '/admin/users' }, fullPath: '/login', meta: { public: true } };
    const loginView = await mountAuthView(pinia);
    loginView.state.email = temporaryUser.email;
    loginView.state.password = 'Temporary!234';
    await loginView.state.submit();

    expect(login).toHaveBeenCalledWith(temporaryUser.email, 'Temporary!234');
    expect(navigation.router.replace).toHaveBeenLastCalledWith('/change-password');
    expect(agents).not.toHaveBeenCalled();
    loginView.app.unmount();

    navigation.route = { name: 'change-password', query: {}, fullPath: '/change-password', meta: {} };
    const passwordView = await mountAuthView(pinia);
    passwordView.state.password = 'Temporary!234';
    passwordView.state.newPassword = 'Permanent!5678';
    passwordView.state.confirmPassword = 'Permanent!5678';
    await passwordView.state.submit();

    expect(changePassword).toHaveBeenCalledWith('Temporary!234', 'Permanent!5678');
    expect(me).toHaveBeenCalledOnce();
    expect(navigation.router.replace).toHaveBeenLastCalledWith('/app');
    expect(agents).not.toHaveBeenCalled();

    const { useAuthStore } = await import('../src/features/auth/auth.store.ts');
    expect(useAuthStore().needsPasswordChange).toBe(false);
    passwordView.app.unmount();
  });

  it('redirects every attempted bypass back to the password-change route', async () => {
    const { resolveAuthNavigation } = await import('../src/features/auth/auth.navigation.ts');
    const auth = {
      isAuthenticated: true,
      isAdmin: true,
      needsPasswordChange: true,
      ensureLoaded: vi.fn().mockResolvedValue(undefined)
    };

    for (const target of [
      { name: 'app', fullPath: '/app', meta: {} },
      { name: 'admin-users', fullPath: '/admin/users', meta: { admin: true } },
      { name: 'login', fullPath: '/login', meta: { public: true } }
    ]) {
      await expect(resolveAuthNavigation(target, auth)).resolves.toEqual({ path: '/change-password' });
    }
    await expect(resolveAuthNavigation(
      { name: 'change-password', fullPath: '/change-password', meta: {} },
      auth
    )).resolves.toBe(true);
  });

  it('does not boot the workbench if a restricted user reaches the App component', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const { useAuthStore } = await import('../src/features/auth/auth.store.ts');
    useAuthStore().user = temporaryUser;
    useAuthStore().loaded = true;
    const { default: App } = await import('../src/app/App.vue');
    const app = renderer.createApp({ ...App, render: () => null });
    app.use(pinia);
    app.provide(ssrContextKey, { modules: new Set() });

    app.mount(hostNode('root'));
    await nextTick();

    expect(workbench.boot).not.toHaveBeenCalled();
    expect(navigation.router.replace).toHaveBeenLastCalledWith('/change-password');
    app.unmount();
  });

  it('allows a restricted user to log out from the forced password screen', async () => {
    const { authApi, api } = await import('../src/shared/services/api.ts');
    const logout = vi.spyOn(authApi, 'logout').mockResolvedValue(undefined);
    const agents = vi.spyOn(api, 'agents');
    const pinia = createPinia();
    setActivePinia(pinia);
    const { useAuthStore } = await import('../src/features/auth/auth.store.ts');
    useAuthStore().user = temporaryUser;
    useAuthStore().loaded = true;
    navigation.route = { name: 'change-password', query: {}, fullPath: '/change-password', meta: {} };
    const passwordView = await mountAuthView(pinia);

    await passwordView.state.logout();

    expect(logout).toHaveBeenCalledOnce();
    expect(useAuthStore().user).toBeNull();
    expect(navigation.router.replace).toHaveBeenLastCalledWith('/login');
    expect(agents).not.toHaveBeenCalled();
    passwordView.app.unmount();
  });
});
