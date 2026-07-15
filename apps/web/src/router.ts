import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore } from './stores/auth';

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/app' },
    { path: '/login', name: 'login', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/register', name: 'register', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/forgot-password', name: 'forgot-password', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/reset-password', name: 'reset-password', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/verify-email', name: 'verify-email', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/app', name: 'app', component: () => import('./App.vue'), meta: { visualMode: 'workspace' } },
    { path: '/admin/users', name: 'admin-users', component: () => import('./views/AdminUsersView.vue'), meta: { admin: true, visualMode: 'admin' } },
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
