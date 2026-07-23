<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue';
import { RouterView, useRoute, useRouter } from 'vue-router';
import { useAuthStore } from './stores/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const surface = computed(() => route.meta.visualMode === 'hero' ? 'auth' : 'product');

watch(surface, (value) => {
  document.body.dataset.surface = value;
}, { immediate: true });

function handleAuthExpired() {
  const redirect = route.fullPath.startsWith('/') && !route.fullPath.startsWith('//') ? route.fullPath : '/app';
  auth.clear();
  void router.replace({ path: '/login', query: { redirect } });
}

onMounted(() => window.addEventListener('contentai:auth-expired', handleAuthExpired));
onBeforeUnmount(() => {
  window.removeEventListener('contentai:auth-expired', handleAuthExpired);
  delete document.body.dataset.surface;
});
</script>

<template>
  <div class="root-shell" :data-surface="surface">
    <RouterView />
  </div>
</template>
