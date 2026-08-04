import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore } from '@/features/auth/auth.store';

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/app' },
    { path: '/login', name: 'login', component: () => import('@/features/auth/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/register', name: 'register', component: () => import('@/features/auth/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/app', name: 'app', component: () => import('@/app/App.vue'), meta: { visualMode: 'workspace' } },
    { path: '/admin/users', name: 'admin-users', component: () => import('@/features/admin/views/AdminUsersView.vue'), meta: { admin: true, visualMode: 'admin' } },
    { path: '/admin/models', name: 'admin-models', component: () => import('@/features/admin/views/AdminModelsView.vue'), meta: { admin: true, visualMode: 'admin' } },
    { path: '/:pathMatch(.*)*', redirect: '/app' }
  ]
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  await auth.ensureLoaded();
  if (to.meta.public) {
    if (auth.isAuthenticated && ['login', 'register'].includes(String(to.name))) return '/app';
    return true;
  }
  if (!auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  if (to.meta.admin && !auth.isAdmin) return '/app';
  return true;
});
