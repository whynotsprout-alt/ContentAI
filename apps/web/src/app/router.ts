import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore } from '@/features/auth/auth.store';
import { resolveAuthNavigation } from '@/features/auth/auth.navigation';

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/app' },
    { path: '/login', name: 'login', component: () => import('@/features/auth/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/register', name: 'register', component: () => import('@/features/auth/AuthView.vue'), meta: { public: true, visualMode: 'hero' } },
    { path: '/change-password', name: 'change-password', component: () => import('@/features/auth/AuthView.vue'), meta: { visualMode: 'hero' } },
    { path: '/app', name: 'app', component: () => import('@/app/App.vue'), meta: { visualMode: 'workspace' } },
    { path: '/admin/users', name: 'admin-users', component: () => import('@/features/admin/views/AdminUsersView.vue'), meta: { admin: true, visualMode: 'admin' } },
    { path: '/admin/models', name: 'admin-models', component: () => import('@/features/admin/views/AdminModelsView.vue'), meta: { admin: true, visualMode: 'admin' } },
    { path: '/:pathMatch(.*)*', redirect: '/app' }
  ]
});

router.beforeEach((to) => resolveAuthNavigation(to, useAuthStore()));
