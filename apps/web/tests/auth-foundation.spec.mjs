import { describe, expect, it } from 'vitest';
import {
  refreshAfterPasswordChange,
  resolveAuthNavigation,
  safeInternalTarget,
  submitPasswordChange
} from '../src/auth/foundation';

describe('authentication foundation behavior', () => {
  it('redirects unauthenticated users and forces only temporary-password users', () => {
    const protectedRoute = { name: 'app', fullPath: '/app', meta: {} };
    const changePassword = { name: 'change-password', fullPath: '/change-password', meta: {} };
    const login = { name: 'login', fullPath: '/login', meta: { public: true } };

    expect(resolveAuthNavigation(protectedRoute, null)).toEqual({ path: '/login', query: { redirect: '/app' } });
    expect(resolveAuthNavigation(protectedRoute, { must_change_password: true, role: 'user' })).toEqual({ path: '/change-password', query: { redirect: '/app' } });
    expect(resolveAuthNavigation(changePassword, { must_change_password: false, role: 'user' })).toBe('/app');
    expect(resolveAuthNavigation(login, { must_change_password: true, role: 'user' })).toBe('/change-password');
  });

  it('accepts only safe internal redirects that do not resolve to password change', () => {
    const resolve = (target) => ({ name: target.startsWith('/change-password') ? 'change-password' : 'app' });

    expect(safeInternalTarget('/admin/users', resolve)).toBe('/admin/users');
    expect(safeInternalTarget('/change-password?redirect=/app', resolve)).toBe('/app');
    expect(safeInternalTarget('/change-password#step', resolve)).toBe('/app');
    expect(safeInternalTarget('//evil.example', resolve)).toBe('/app');
  });

  it('fails closed when refreshing the current user after a successful password update fails', async () => {
    let cleared = 0;
    await expect(refreshAfterPasswordChange({
      refresh: async () => { throw new Error('network unavailable'); },
      clear: () => { cleared += 1; }
    })).rejects.toThrow('network unavailable');
    expect(cleared).toBe(1);
  });

  it('prevents duplicate password submissions while the first request is pending', async () => {
    let resolveRequest;
    let calls = 0;
    let changed = 0;
    const state = { busy: false, error: '' };
    const first = submitPasswordChange(state, {
      currentPassword: 'temporary-password',
      newPassword: 'new-password-123',
      confirmPassword: 'new-password-123'
    }, {
      change: async () => {
        calls += 1;
        await new Promise((resolve) => { resolveRequest = resolve; });
      },
      changed: async () => { changed += 1; }
    });

    expect(state.busy).toBe(true);
    expect(await submitPasswordChange(state, {
      currentPassword: 'temporary-password',
      newPassword: 'new-password-123',
      confirmPassword: 'new-password-123'
    }, { change: async () => { calls += 1; }, changed: async () => { changed += 1; } })).toBe(false);
    expect(calls).toBe(1);
    resolveRequest();
    expect(await first).toBe(true);
    expect(changed).toBe(1);
    expect(state.busy).toBe(false);
  });
});
