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
  <main class="auth-shell">
    <section class="auth-access">
      <div class="auth-form-card liquid-glass-strong">
        <ChangePasswordForm forced :on-changed="changed" />
      </div>
    </section>
  </main>
</template>
