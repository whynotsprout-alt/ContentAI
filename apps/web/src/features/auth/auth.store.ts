import { defineStore } from 'pinia';
import { authApi, type CurrentUser } from '@/shared/services/api';

let ensureLoadedPromise: Promise<void> | null = null;

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as CurrentUser | null,
    loaded: false,
    loading: false
  }),
  getters: {
    isAuthenticated: (state) => Boolean(state.user),
    isAdmin: (state) => state.user?.role === 'admin'
  },
  actions: {
    async ensureLoaded() {
      if (this.loaded) return;
      if (ensureLoadedPromise) return ensureLoadedPromise;
      this.loading = true;
      ensureLoadedPromise = (async () => {
        try {
          this.user = await authApi.me();
        } catch {
          this.user = null;
        } finally {
          this.loading = false;
          this.loaded = true;
        }
      })().finally(() => {
        ensureLoadedPromise = null;
      });
      return ensureLoadedPromise;
    },
    async login(email: string, password: string) {
      this.user = await authApi.login(email, password);
      this.loaded = true;
    },
    async logout() {
      try {
        await authApi.logout();
      } finally {
        this.user = null;
        this.loaded = true;
      }
    },
    clear() {
      this.user = null;
      this.loaded = true;
    }
  }
});
