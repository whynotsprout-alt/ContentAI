<script setup lang="ts">
import { ArrowLeft, Boxes, LogOut, ShieldCheck, Users } from '@lucide/vue';
import { nextTick, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '../stores/auth';

defineProps<{
  title: string;
  description: string;
}>();

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const mainContent = ref<HTMLElement | null>(null);

async function focusAdminContent() {
  await nextTick();
  mainContent.value?.focus({ preventScroll: true });
}

onMounted(() => void focusAdminContent());
watch(() => route.fullPath, () => void focusAdminContent(), { flush: 'post' });

async function logout() {
  await auth.logout();
  await router.replace('/login');
}
</script>

<template>
  <main class="admin-shell product-shell" aria-labelledby="admin-page-title">
    <a class="skip-link" href="#admin-main-content" @click="focusAdminContent">跳到管理内容</a>
    <header class="admin-topbar">
      <div class="admin-brand">
        <span class="brand-symbol"><ShieldCheck :size="18" /></span>
        <div><strong>ContentAI 管理后台</strong><small>用户、模型与真实用量</small></div>
      </div>
      <nav class="admin-primary-nav" aria-label="管理后台导航">
        <RouterLink to="/admin/users" :aria-current="route.name === 'admin-users' ? 'page' : undefined">
          <Users :size="16" /><span class="admin-nav-label">用户管理</span>
        </RouterLink>
        <RouterLink to="/admin/models" :aria-current="route.name === 'admin-models' ? 'page' : undefined">
          <Boxes :size="16" /><span class="admin-nav-label">模型管理</span>
        </RouterLink>
      </nav>
      <nav class="admin-utility-nav" aria-label="账户操作">
        <button type="button" aria-label="返回工作台" @click="router.push('/app')"><ArrowLeft :size="16" /><span class="admin-nav-label">返回工作台</span></button>
        <button type="button" aria-label="退出管理后台" @click="logout"><LogOut :size="16" /><span class="admin-nav-label">退出</span></button>
      </nav>
    </header>

    <section id="admin-main-content" ref="mainContent" class="admin-content" tabindex="-1">
      <header class="admin-heading">
        <div>
          <span class="section-kicker">ADMINISTRATION</span>
          <h1 id="admin-page-title">{{ title }}</h1>
          <p>{{ description }}</p>
        </div>
        <slot name="heading-status" />
      </header>
      <slot />
    </section>
  </main>
</template>
