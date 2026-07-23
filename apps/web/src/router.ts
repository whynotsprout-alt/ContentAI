import { createRouter, createWebHistory } from 'vue-router';
import { resolveAuthNavigation } from './auth/foundation';
import { useAuthStore } from './stores/auth';

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/app' },
    { path: '/login', name: 'login', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/register', name: 'register', component: () => import('./views/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/change-password', name: 'change-password', component: () => import('./views/ChangePasswordView.vue'), meta: { visualMode: 'workspace' } },
    { path: '/app', name: 'app', component: () => import('./App.vue'), meta: { visualMode: 'workspace' } },
    { path: '/admin/users', name: 'admin-users', component: () => import('./views/AdminUsersView.vue'), meta: { admin: true, visualMode: 'admin' } },
    { path: '/admin/models', name: 'admin-models', component: () => import('./views/AdminModelsView.vue'), meta: { admin: true, visualMode: 'admin' } },
    { path: '/:pathMatch(.*)*', redirect: '/login' }
  ]
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  await auth.ensureLoaded();
  return resolveAuthNavigation(to, auth.user);
});
