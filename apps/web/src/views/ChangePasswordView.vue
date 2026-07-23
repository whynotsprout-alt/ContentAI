<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router';
import ChangePasswordForm from '../components/ChangePasswordForm.vue';
import { useAuthStore } from '../stores/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

function safeInternalTarget(value: unknown) {
  const target = typeof value === 'string' ? value : '';
  return target.startsWith('/') && !target.startsWith('//') && target !== '/change-password' ? target : '/app';
}

async function changed() {
  await auth.refresh();
  await router.replace(safeInternalTarget(route.query.redirect));
}
</script>

<template>
  <main class="auth-shell">
    <section class="auth-access">
      <div class="auth-form-card liquid-glass-strong">
        <ChangePasswordForm forced @changed="changed" />
      </div>
    </section>
  </main>
</template>
