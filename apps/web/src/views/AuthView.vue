<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ArrowRight, CheckCircle2, Eye, EyeOff, LoaderCircle, Mail, RefreshCw } from '@lucide/vue';
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
const message = ref('');
const registrationSent = ref(false);
const resendCooldown = ref(0);
let cooldownTimer = 0;

const mode = computed(() => String(route.name ?? 'login'));
const isLogin = computed(() => mode.value === 'login');
const isRegister = computed(() => mode.value === 'register');
const isForgot = computed(() => mode.value === 'forgot-password');
const isReset = computed(() => mode.value === 'reset-password');
const isVerify = computed(() => mode.value === 'verify-email');
const title = computed(() => {
  if (isRegister.value) return '创建你的内容空间';
  if (isForgot.value) return '找回账号访问权';
  if (isReset.value) return '设置新密码';
  if (isVerify.value) return '验证邮箱';
  return '欢迎回到 ContentAI';
});
const description = computed(() => {
  if (isRegister.value) return '创建独立账号，管理属于你的内容配置和历史对话。';
  if (isForgot.value) return '输入注册邮箱，我们会发送一次性密码重置链接。';
  if (isReset.value) return '密码更新后，其他已登录设备会自动退出。';
  if (isVerify.value) return '正在确认验证链接，请稍候。';
  return '登录后继续管理内容账号和对话。';
});

function errorText(value: unknown) {
  if (value instanceof ApiError) {
    return `${value.message}${value.requestId ? `（请求标识：${value.requestId}）` : ''}`;
  }
  return value instanceof Error ? value.message : String(value || '请求失败');
}

function startCooldown(seconds = 60) {
  window.clearInterval(cooldownTimer);
  resendCooldown.value = seconds;
  cooldownTimer = window.setInterval(() => {
    resendCooldown.value = Math.max(0, resendCooldown.value - 1);
    if (!resendCooldown.value) window.clearInterval(cooldownTimer);
  }, 1000);
}

async function submit() {
  error.value = '';
  message.value = '';
  if ((isRegister.value || isReset.value) && password.value !== confirmPassword.value) {
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
      registrationSent.value = true;
      message.value = result.message || '验证邮件已发送。';
      password.value = '';
      confirmPassword.value = '';
      startCooldown();
    } else if (isForgot.value) {
      const result = await authApi.forgotPassword(email.value);
      message.value = result.message;
    } else if (isReset.value) {
      const token = String(route.query.token || '');
      if (!token) throw new Error('缺少密码重置令牌。');
      const result = await authApi.resetPassword(token, password.value);
      message.value = result.message;
      window.setTimeout(() => void router.replace('/login'), 900);
    }
  } catch (value) {
    error.value = errorText(value);
  } finally {
    loading.value = false;
  }
}

async function resendVerification() {
  if (!email.value || loading.value || resendCooldown.value) return;
  loading.value = true;
  error.value = '';
  message.value = '';
  try {
    const result = await authApi.resendVerification(email.value);
    message.value = result.message || '验证邮件已重新发送。';
    startCooldown();
  } catch (value) {
    error.value = errorText(value);
  } finally {
    loading.value = false;
  }
}

async function verify() {
  if (!isVerify.value) return;
  const token = String(route.query.token || '');
  if (!token) {
    error.value = '缺少邮箱验证令牌。';
    return;
  }
  loading.value = true;
  try {
    await authApi.verifyEmail(token);
    message.value = '邮箱验证成功，现在可以登录。';
  } catch (value) {
    error.value = errorText(value);
  } finally {
    loading.value = false;
  }
}

watch(() => route.name, () => {
  error.value = '';
  message.value = '';
  registrationSent.value = false;
  resendCooldown.value = 0;
  password.value = '';
  confirmPassword.value = '';
  showPassword.value = false;
  showConfirmPassword.value = false;
  window.clearInterval(cooldownTimer);
  void verify();
});

onMounted(() => {
  void verify();
});

onBeforeUnmount(() => {
  window.clearInterval(cooldownTimer);
});
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

          <div v-if="isVerify" class="auth-state" :class="{ success: message }">
            <LoaderCircle v-if="loading" :size="28" class="spin" />
            <CheckCircle2 v-else-if="message" :size="30" />
            <Mail v-else :size="30" />
            <p v-if="message">{{ message }}</p>
            <p v-else-if="error" class="auth-error" role="alert">{{ error }}</p>
            <RouterLink v-if="!loading" class="auth-link-button" to="/login">前往登录</RouterLink>
          </div>

          <div v-else-if="registrationSent" class="auth-state email-sent">
            <span class="auth-state-icon"><Mail :size="25" /></span>
            <h3>检查你的邮箱</h3>
            <p>验证链接已发送至 <strong>{{ email }}</strong>。完成验证后即可登录。</p>
            <p v-if="message" class="auth-success" role="status">{{ message }}</p>
            <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
            <button class="auth-submit secondary" type="button" :disabled="loading || resendCooldown > 0" @click="resendVerification">
              <LoaderCircle v-if="loading" :size="17" class="spin" />
              <RefreshCw v-else :size="17" />
              {{ resendCooldown ? `${resendCooldown} 秒后可重发` : '重新发送验证邮件' }}
            </button>
            <RouterLink class="auth-link-button" to="/login">返回登录</RouterLink>
          </div>

          <template v-else>
            <label v-if="!isReset" class="field-block">
              <span>邮箱</span>
              <input v-model="email" type="email" autocomplete="email" required placeholder="name@example.com" />
            </label>
            <label v-if="!isForgot" class="field-block">
              <span>{{ isLogin ? '密码' : '新密码' }}</span>
              <span class="password-field">
                <input v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="isLogin ? 'current-password' : 'new-password'" required :minlength="isLogin ? 1 : 10" maxlength="128" placeholder="至少 10 个字符" />
                <button type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" @click="showPassword = !showPassword">
                  <EyeOff v-if="showPassword" :size="18" />
                  <Eye v-else :size="18" />
                </button>
              </span>
            </label>
            <label v-if="isRegister || isReset" class="field-block">
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
              <span v-else>{{ isLogin ? '登录' : isRegister ? '创建账号' : isForgot ? '发送重置邮件' : '保存新密码' }}</span>
              <ArrowRight v-if="!loading" :size="18" />
            </button>
            <nav class="auth-links" aria-label="认证辅助链接">
              <RouterLink v-if="isLogin" to="/forgot-password">忘记密码</RouterLink>
              <span v-if="isLogin" class="auth-links__alternate">
                <span>还没有账号？</span>
                <RouterLink to="/register">创建账号</RouterLink>
              </span>
              <RouterLink v-else to="/login">返回登录</RouterLink>
            </nav>
          </template>
          </form>
        </div>
      </div>
    </section>
  </main>
</template>
