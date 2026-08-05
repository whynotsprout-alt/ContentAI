export interface AuthNavigationTarget {
  name?: unknown;
  fullPath: string;
  meta: {
    public?: unknown;
    admin?: unknown;
  };
}

export interface AuthNavigationState {
  isAuthenticated: boolean;
  isAdmin: boolean;
  needsPasswordChange: boolean;
  ensureLoaded: () => Promise<unknown>;
}

export async function resolveAuthNavigation(
  to: AuthNavigationTarget,
  auth: AuthNavigationState
) {
  await auth.ensureLoaded();
  const routeName = String(to.name ?? '');

  if (auth.needsPasswordChange) {
    return routeName === 'change-password' ? true : { path: '/change-password' };
  }

  if (routeName === 'change-password') return '/app';

  if (to.meta.public) {
    if (auth.isAuthenticated && ['login', 'register'].includes(routeName)) return '/app';
    return true;
  }

  if (!auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  if (to.meta.admin && !auth.isAdmin) return '/app';
  return true;
}
