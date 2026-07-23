export type NavigationRoute = {
  name?: string | symbol | null;
  fullPath: string;
  meta: { public?: boolean; admin?: boolean };
};

export type AuthNavigationUser = {
  must_change_password: boolean;
  role: 'user' | 'admin';
};

export function resolveAuthNavigation(route: NavigationRoute, user: AuthNavigationUser | null) {
  const routeName = String(route.name ?? '');
  if (route.meta.public) {
    if (!user) return true;
    if (routeName === 'login' || routeName === 'register') {
      return user.must_change_password ? '/change-password' : '/app';
    }
    return true;
  }
  if (!user) return { path: '/login', query: { redirect: route.fullPath } };
  if (user.must_change_password && routeName !== 'change-password') {
    return { path: '/change-password', query: { redirect: route.fullPath } };
  }
  if (!user.must_change_password && routeName === 'change-password') return '/app';
  if (route.meta.admin && user.role !== 'admin') return '/app';
  return true;
}

export function safeInternalTarget(
  value: unknown,
  resolve: (target: string) => { name?: string | symbol | null }
) {
  const target = typeof value === 'string' ? value : '';
  if (!target.startsWith('/') || target.startsWith('//')) return '/app';
  return String(resolve(target).name ?? '') === 'change-password' ? '/app' : target;
}

export async function refreshAfterPasswordChange({
  refresh,
  clear
}: {
  refresh: () => Promise<unknown>;
  clear: () => void;
}) {
  try {
    await refresh();
  } catch (error) {
    clear();
    throw error;
  }
}

export type PasswordSubmissionState = { busy: boolean; error: string };

export async function submitPasswordChange(
  state: PasswordSubmissionState,
  values: { currentPassword: string; newPassword: string; confirmPassword: string },
  actions: {
    change: (currentPassword: string, newPassword: string) => Promise<unknown>;
    changed: () => Promise<unknown>;
    busyChanged?: (busy: boolean) => void;
  }
) {
  if (state.busy) return false;
  state.error = '';
  if (values.newPassword.length < 10) {
    state.error = '新密码至少需要 10 个字符。';
    return false;
  }
  if (values.newPassword !== values.confirmPassword) {
    state.error = '两次输入的新密码不一致。';
    return false;
  }
  state.busy = true;
  actions.busyChanged?.(true);
  try {
    await actions.change(values.currentPassword, values.newPassword);
    await actions.changed();
    return true;
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
    return false;
  } finally {
    state.busy = false;
    actions.busyChanged?.(false);
  }
}
