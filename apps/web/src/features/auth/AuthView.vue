<script setup lang="ts">
import { computed, ref } from 'vue';
import { ArrowRight, Eye, EyeOff, LoaderCircle, LogOut, ShieldCheck } from '@lucide/vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { ApiError, authApi } from '@/shared/services/api';
import { useAuthStore } from '@/features/auth/auth.store';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const email = ref('');
const password = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const showPassword = ref(false);
const showNewPassword = ref(false);
const showConfirmPassword = ref(false);
const loading = ref(false);
const error = ref('');
const message = ref('');

const mode = computed(() => String(route.name ?? 'login'));
const isLogin = computed(() => mode.value === 'login');
const isRegister = computed(() => mode.value === 'register');
const isPasswordChange = computed(() => mode.value === 'change-password');
const title = computed(() => {
  if (isRegister.value) return '创建你的内容空间';
  if (isPasswordChange.value) return '先设置一个新密码';
  return '欢迎回到 ContentAI';
});
const description = computed(() => {
  if (isRegister.value) return '创建独立账号，管理属于你的内容配置和历史对话。';
  if (isPasswordChange.value) return '当前密码是管理员签发的临时凭证。完成改密后才能进入工作台。';
  return '登录后继续管理内容账号和对话。';
});
const submitLabel = computed(() => {
  if (isRegister.value) return '创建账号';
  if (isPasswordChange.value) return '保存新密码并进入';
  return '登录';
});
const temporaryPasswordExpiry = computed(() => {
  const value = auth.user?.temporary_password_expires_at;
  if (!value) return '';
  const expiry = new Date(value);
  if (Number.isNaN(expiry.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(expiry);
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
  if (isPasswordChange.value && newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致。';
    return;
  }
  if (isPasswordChange.value && password.value === newPassword.value) {
    error.value = '新密码不能与临时密码相同。';
    return;
  }
  loading.value = true;
  try {
    if (isLogin.value) {
      await auth.login(email.value, password.value);
      await router.replace(auth.needsPasswordChange ? '/change-password' : String(route.query.redirect || '/app'));
    } else if (isRegister.value) {
      const result = await authApi.register(email.value, password.value);
      message.value = result.message;
      password.value = '';
      confirmPassword.value = '';
      await router.replace('/login');
    } else if (isPasswordChange.value) {
      await auth.changePassword(password.value, newPassword.value);
      if (auth.needsPasswordChange) {
        error.value = '密码状态尚未更新，请重试或退出后重新登录。';
        return;
      }
      password.value = '';
      newPassword.value = '';
      confirmPassword.value = '';
      await router.replace('/app');
    }
  } catch (value) {
    error.value = errorText(value);
  } finally {
    loading.value = false;
  }
}

async function logout() {
  error.value = '';
  loading.value = true;
  try {
    await auth.logout();
    await router.replace('/login');
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
        <h1 id="auth-hero-title">从热点判断到<em>完整表达</em>，<br class="auth-hero-break" />都在一段对话里。</h1>
        <p>让每个内容账号拥有清晰的来源偏好、判断标准与表达方式。</p>
      </div>

      <div class="auth-access">
        <div class="auth-form-card liquid-glass-strong">
          <form class="auth-form" @submit.prevent="submit">
          <header>
            <span class="section-kicker">{{ isPasswordChange ? 'SECURITY CHECK' : 'YOUR WORKSPACE' }}</span>
            <h2 id="auth-form-title">{{ title }}</h2>
            <p>{{ description }}</p>
          </header>

            <div v-if="isPasswordChange" class="auth-password-notice" role="status">
              <ShieldCheck :size="20" aria-hidden="true" />
              <span>
                <strong>工作台暂时锁定</strong>
                <small v-if="temporaryPasswordExpiry">临时密码有效期至 {{ temporaryPasswordExpiry }}</small>
                <small v-else>修改成功前不会加载内容账号或会话数据</small>
              </span>
            </div>

            <label v-if="!isPasswordChange" class="field-block">
              <span>邮箱</span>
              <input v-model="email" type="email" autocomplete="email" required placeholder="name@example.com" />
            </label>
            <label class="field-block">
              <span>{{ isPasswordChange ? '当前临时密码' : isLogin ? '密码' : '新密码' }}</span>
              <span class="password-field">
                <input v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="isLogin || isPasswordChange ? 'current-password' : 'new-password'" required :minlength="isLogin || isPasswordChange ? 1 : 10" maxlength="128" :placeholder="isPasswordChange ? '输入管理员提供的临时密码' : isLogin ? '输入密码' : '至少 10 个字符'" />
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
            <label v-if="isPasswordChange" class="field-block">
              <span>新密码</span>
              <span class="password-field">
                <input v-model="newPassword" :type="showNewPassword ? 'text' : 'password'" autocomplete="new-password" required minlength="10" maxlength="128" placeholder="至少 10 个字符" />
                <button type="button" :aria-label="showNewPassword ? '隐藏新密码' : '显示新密码'" :aria-pressed="showNewPassword" @click="showNewPassword = !showNewPassword">
                  <EyeOff v-if="showNewPassword" :size="18" />
                  <Eye v-else :size="18" />
                </button>
              </span>
            </label>
            <label v-if="isPasswordChange" class="field-block">
              <span>确认新密码</span>
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
              <span v-else>{{ submitLabel }}</span>
              <ArrowRight v-if="!loading" :size="18" />
            </button>
            <nav v-if="isPasswordChange" class="auth-links auth-links--forced" aria-label="强制改密辅助操作">
              <span class="auth-links__alternate">当前账号：{{ auth.user?.email }}</span>
              <button class="auth-link-button" type="button" :disabled="loading" @click="logout">
                <LogOut :size="14" aria-hidden="true" />
                退出登录
              </button>
            </nav>
            <nav v-else class="auth-links" aria-label="认证辅助链接">
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
