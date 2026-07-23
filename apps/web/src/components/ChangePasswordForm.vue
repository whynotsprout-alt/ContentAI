<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { LoaderCircle } from '@lucide/vue';
import { submitPasswordChange } from '../auth/foundation';
import { authApi } from '../services/api';

const props = defineProps<{ forced?: boolean; titleId?: string; headingLevel?: 1 | 2; onChanged?: () => Promise<void> }>();
const emit = defineEmits<{ changed: []; close: []; busy: [value: boolean] }>();

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const state = reactive({ busy: false, error: '' });
const headingTag = computed(() => `h${props.headingLevel ?? 2}`);

async function submit() {
  await submitPasswordChange(state, {
    currentPassword: currentPassword.value,
    newPassword: newPassword.value,
    confirmPassword: confirmPassword.value
  }, {
    change: (currentPassword, newPassword) => authApi.changePassword(currentPassword, newPassword),
    changed: async () => {
      if (props.onChanged) await props.onChanged();
      else emit('changed');
    },
    busyChanged: (busy) => emit('busy', busy)
  });
}
</script>

<template>
  <form class="password-dialog" @submit.prevent="submit">
    <button v-if="!forced" class="dialog-close" type="button" aria-label="关闭" :disabled="state.busy" @click="emit('close')">×</button>
    <component :is="headingTag" :id="titleId">修改密码</component>
    <p v-if="forced">为保障账号安全，请先修改临时密码。</p>
    <p v-else>更新后，其他已登录设备会自动退出。</p>
    <label class="field-block"><span>当前密码</span><input v-model="currentPassword" type="password" autocomplete="current-password" required /></label>
    <label class="field-block"><span>新密码</span><input v-model="newPassword" type="password" autocomplete="new-password" minlength="10" required /></label>
    <label class="field-block"><span>确认新密码</span><input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="10" required /></label>
    <p v-if="state.error" class="form-feedback error" role="alert">{{ state.error }}</p>
    <div class="dialog-actions">
      <button v-if="!forced" type="button" :disabled="state.busy" @click="emit('close')">取消</button>
      <button class="primary-action" type="submit" :disabled="state.busy"><LoaderCircle v-if="state.busy" :size="16" class="spin" />保存新密码</button>
    </div>
  </form>
</template>
