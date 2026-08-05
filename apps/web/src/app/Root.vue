<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue';
import { RouterView, useRoute, useRouter } from 'vue-router';
import AmbientBackdrop from '@/features/workbench/components/AmbientBackdrop.vue';
import { useAuthStore } from '@/features/auth/auth.store';

type AmbientBackdropMode = 'hero' | 'workspace' | 'admin';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const visualMode = computed<AmbientBackdropMode>(() => {
  const mode = route.meta.visualMode;
  return mode === 'hero' || mode === 'admin' ? mode : 'workspace';
});

function handleAuthExpired() {
  auth.clear();
  if (route.path === '/login' || route.path === '/register') return;
  void router.replace({ path: '/login', query: { redirect: route.fullPath } });
}

onMounted(() => window.addEventListener('contentai:auth-expired', handleAuthExpired));
onBeforeUnmount(() => window.removeEventListener('contentai:auth-expired', handleAuthExpired));
</script>

<template>
  <div class="root-shell" :data-visual-mode="visualMode">
    <AmbientBackdrop :mode="visualMode" />
    <div class="desktop-application">
      <RouterView />
    </div>
  </div>
</template>
