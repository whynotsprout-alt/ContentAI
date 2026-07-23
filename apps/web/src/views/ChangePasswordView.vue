<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router';
import { refreshAfterPasswordChange, safeInternalTarget } from '../auth/foundation';
import ChangePasswordForm from '../components/ChangePasswordForm.vue';
import { useAuthStore } from '../stores/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

async function changed() {
  try {
    await refreshAfterPasswordChange({ refresh: () => auth.refresh(), clear: () => auth.clear() });
    await router.replace(safeInternalTarget(route.query.redirect, router.resolve));
  } catch {
    await router.replace({ path: '/login', query: { notice: 'password-changed' } });
  }
}
</script>

<template>
  <main class="product-shell password-page">
    <section class="password-panel" aria-label="账号安全">
      <ChangePasswordForm forced :on-changed="changed" />
    </section>
  </main>
</template>
