<script setup lang="ts">
import { ref } from 'vue';
import { LoaderCircle } from '@lucide/vue';
import { authApi } from '../services/api';

defineProps<{ forced?: boolean; titleId?: string }>();
const emit = defineEmits<{ changed: []; close: [] }>();

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const busy = ref(false);
const error = ref('');

async function submit() {
  error.value = '';
  if (newPassword.value.length < 10) {
    error.value = '新密码至少需要 10 个字符。';
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致。';
    return;
  }
  busy.value = true;
  try {
    await authApi.changePassword(currentPassword.value, newPassword.value);
    emit('changed');
  } catch (value) {
    error.value = value instanceof Error ? value.message : String(value);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form class="password-dialog" @submit.prevent="submit">
    <button v-if="!forced" class="dialog-close" type="button" aria-label="关闭" @click="emit('close')">×</button>
    <h2 :id="titleId">修改密码</h2>
    <p v-if="forced">为保障账号安全，请先修改临时密码。</p>
    <p v-else>更新后，其他已登录设备会自动退出。</p>
    <label class="field-block"><span>当前密码</span><input v-model="currentPassword" type="password" autocomplete="current-password" required /></label>
    <label class="field-block"><span>新密码</span><input v-model="newPassword" type="password" autocomplete="new-password" minlength="10" required /></label>
    <label class="field-block"><span>确认新密码</span><input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="10" required /></label>
    <p v-if="error" class="form-feedback error" role="alert">{{ error }}</p>
    <div class="dialog-actions">
      <button v-if="!forced" type="button" @click="emit('close')">取消</button>
      <button class="primary-action" type="submit" :disabled="busy"><LoaderCircle v-if="busy" :size="16" class="spin" />保存新密码</button>
    </div>
  </form>
</template>
