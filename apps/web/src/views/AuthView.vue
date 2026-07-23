<script setup lang="ts">
import { computed, ref } from 'vue';
import { ArrowRight, Eye, EyeOff, LoaderCircle } from '@lucide/vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { ApiError, authApi } from '../services/api';
import { useAuthStore } from '../stores/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const email = ref('');
const password = ref('');
const confirmPassword = ref('');
const showPassword = ref(false);
const showConfirmPassword = ref(false);
const loading = ref(false);
const error = ref('');
const message = ref(route.query.notice === 'password-changed' ? '密码已更新，请重新登录。' : '');

const mode = computed(() => String(route.name ?? 'login'));
const isLogin = computed(() => mode.value === 'login');
const isRegister = computed(() => mode.value === 'register');
const title = computed(() => {
  if (isRegister.value) return '创建你的内容空间';
  return '欢迎回到 ContentAI';
});
const description = computed(() => {
  if (isRegister.value) return '创建独立账号，管理属于你的内容配置和历史对话。';
  return '登录后继续管理内容账号和对话。';
});

function errorText(value: unknown) {
  if (value instanceof ApiError) {
    return `${value.message}${value.requestId ? `（请求标识：${value.requestId}）` : ''}`;
  }
  return value instanceof Error ? value.message : String(value || '请求失败');
}

async function submit() {
  error.value = '';
  message.value = '';
  if (isRegister.value && password.value !== confirmPassword.value) {
    error.value = '两次输入的密码不一致。';
    return;
  }
  loading.value = true;
  try {
    if (isLogin.value) {
      await auth.login(email.value, password.value);
      await router.replace(String(route.query.redirect || '/app'));
    } else if (isRegister.value) {
      const result = await authApi.register(email.value, password.value);
      message.value = result.message;
      password.value = '';
      confirmPassword.value = '';
      await router.replace('/login');
    }
  } catch (value) {
    error.value = errorText(value);
  } finally {
    loading.value = false;
  }
}

</script>

<template>
  <main class="auth-shell">
    <section class="auth-hero" aria-labelledby="auth-hero-title">
      <div class="auth-hero__copy">
        <span class="section-kicker">CONTENT INTELLIGENCE</span>
        <h1 id="auth-hero-title">从热点判断到<em>完整表达</em>，<br />都在一段对话里。</h1>
        <p>让每个内容账号拥有清晰的来源偏好、判断标准与表达方式。</p>
      </div>

      <div class="auth-access">
        <div class="auth-form-card liquid-glass-strong">
          <form class="auth-form" @submit.prevent="submit">
          <header>
            <span class="section-kicker">YOUR WORKSPACE</span>
            <h2 id="auth-form-title">{{ title }}</h2>
            <p>{{ description }}</p>
          </header>

            <label class="field-block">
              <span>邮箱</span>
              <input v-model="email" type="email" autocomplete="email" required placeholder="name@example.com" />
            </label>
            <label class="field-block">
              <span>{{ isLogin ? '密码' : '新密码' }}</span>
              <span class="password-field">
                <input v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="isLogin ? 'current-password' : 'new-password'" required :minlength="isLogin ? 1 : 10" maxlength="128" placeholder="至少 10 个字符" />
                <button type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" @click="showPassword = !showPassword">
                  <EyeOff v-if="showPassword" :size="18" />
                  <Eye v-else :size="18" />
                </button>
              </span>
            </label>
            <label v-if="isRegister" class="field-block">
              <span>确认密码</span>
              <span class="password-field">
                <input v-model="confirmPassword" :type="showConfirmPassword ? 'text' : 'password'" autocomplete="new-password" required minlength="10" maxlength="128" />
                <button type="button" :aria-label="showConfirmPassword ? '隐藏确认密码' : '显示确认密码'" :aria-pressed="showConfirmPassword" @click="showConfirmPassword = !showConfirmPassword">
                  <EyeOff v-if="showConfirmPassword" :size="18" />
                  <Eye v-else :size="18" />
                </button>
              </span>
            </label>

            <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
            <p v-if="message" class="auth-success" role="status">{{ message }}</p>
            <button class="auth-submit" type="submit" :disabled="loading">
              <LoaderCircle v-if="loading" :size="18" class="spin" />
              <span v-else>{{ isLogin ? '登录' : '创建账号' }}</span>
              <ArrowRight v-if="!loading" :size="18" />
            </button>
            <nav class="auth-links" aria-label="认证辅助链接">
              <span v-if="isLogin" class="auth-links__alternate">
                <span>还没有账号？</span>
                <RouterLink to="/register">创建账号</RouterLink>
              </span>
              <RouterLink v-else to="/login">返回登录</RouterLink>
            </nav>
          </form>
        </div>
      </div>
    </section>
  </main>
</template>
