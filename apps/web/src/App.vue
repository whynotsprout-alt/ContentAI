<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { AlertTriangle, KeyRound, LoaderCircle, Trash2, X } from '@lucide/vue';
import { useRouter } from 'vue-router';
import type { ChatSessionSummary } from './services/api';
import { authApi } from './services/api';
import { useAuthStore } from './stores/auth';
import { useWorkbenchStore } from './stores/workbench';
import AccessibleDialog from './components/AccessibleDialog.vue';
import AgentManager from './components/AgentManager.vue';
import ChatCanvas from './components/ChatCanvas.vue';
import RunActivityBar from './components/RunActivityBar.vue';
import SessionRail from './components/SessionRail.vue';
import WorkbenchHeader from './components/WorkbenchHeader.vue';

const router = useRouter();
const auth = useAuthStore();
const store = useWorkbenchStore();

const agentManagerOpen = ref(false);
const initialAgentTemplate = ref<'finance' | 'ai' | null>(null);
const deleteTarget = ref<ChatSessionSummary | null>(null);
const deleteBusy = ref(false);
const changePasswordOpen = ref(false);
const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const passwordBusy = ref(false);
const passwordError = ref('');
const passwordNotice = ref('');
let agentManagerReturnFocus: HTMLElement | null = null;

const interruptSummary = computed(() => {
  const payload = store.interruptPayload;
  const interrupts = Array.isArray(payload.interrupts) ? payload.interrupts : [];
  const first = interrupts[0] as { value?: unknown } | undefined;
  if (!first?.value) return 'Agent 需要你的确认或补充信息后继续。';
  return typeof first.value === 'string' ? first.value : JSON.stringify(first.value);
});

function openAgentManager(template: 'finance' | 'ai' | null = null) {
  agentManagerReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  initialAgentTemplate.value = template;
  agentManagerOpen.value = true;
}

function closeAgentManager() {
  agentManagerOpen.value = false;
  initialAgentTemplate.value = null;
  const target = agentManagerReturnFocus;
  agentManagerReturnFocus = null;
  void nextTick(() => {
    if (target?.isConnected) {
      target.focus();
      return;
    }
    const triggers = Array.from(document.querySelectorAll<HTMLElement>('[data-agent-manager-trigger]'));
    triggers.find((element) => element.offsetParent !== null)?.focus();
  });
}

async function chooseAgent(agentId: string) {
  await store.chooseAgent(agentId);
}

async function createSession() {
  await store.startNewSession();
}

async function loadSession(sessionId: string) {
  await store.loadSession(sessionId);
}

function requestDeleteSession(session: ChatSessionSummary) {
  deleteTarget.value = session;
}

async function deleteSession() {
  if (!deleteTarget.value) return;
  const sessionId = deleteTarget.value.session_id;
  deleteBusy.value = true;
  store.error = '';
  store.lastErrorCode = '';
  await store.deleteSession(sessionId);
  deleteBusy.value = false;
  if (!store.sessions.some((session) => session.session_id === sessionId)) deleteTarget.value = null;
}

async function submitMessage(message: string) {
  return store.submit(message);
}

async function resumeRun(message: string) {
  return store.resume(message);
}

async function cancelRun() {
  await store.cancelActiveRun();
}

function openPasswordDialog() {
  currentPassword.value = '';
  newPassword.value = '';
  confirmPassword.value = '';
  passwordError.value = '';
  passwordNotice.value = '';
  changePasswordOpen.value = true;
}

async function changePassword() {
  passwordError.value = '';
  passwordNotice.value = '';
  if (newPassword.value.length < 10) {
    passwordError.value = '新密码至少需要 10 个字符。';
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = '两次输入的新密码不一致。';
    return;
  }
  passwordBusy.value = true;
  try {
    const result = await authApi.changePassword(currentPassword.value, newPassword.value);
    passwordNotice.value = result.message || '密码已更新。';
    currentPassword.value = '';
    newPassword.value = '';
    confirmPassword.value = '';
  } catch (value) {
    passwordError.value = value instanceof Error ? value.message : String(value);
  } finally {
    passwordBusy.value = false;
  }
}

async function logout() {
  await auth.logout();
  await router.replace('/login');
}

function handleAuthExpired() {
  auth.clear();
  void router.replace({ path: '/login', query: { redirect: '/app' } });
}

onMounted(() => {
  window.addEventListener('contentai:auth-expired', handleAuthExpired);
  void store.boot().catch((value) => {
    store.error = value instanceof Error ? value.message : String(value);
  });
});

onBeforeUnmount(() => {
  window.removeEventListener('contentai:auth-expired', handleAuthExpired);
  store.eventSource?.close();
});
</script>

<template>
  <main class="workbench-shell">
    <a class="skip-link" href="#main-content">跳到对话区</a>

    <WorkbenchHeader
      :agents="store.agents"
      :active-agent-id="store.agentId"
      :user-email="auth.user?.email ?? ''"
      :is-admin="auth.isAdmin"
      :switching="store.isSwitchingAgent"
      @choose-agent="chooseAgent"
      @open-agents="openAgentManager()"
      @open-admin="router.push('/admin/users')"
      @change-password="openPasswordDialog"
      @logout="logout"
    />

    <div class="workbench-grid">
      <SessionRail
        :sessions="store.sessions"
        :active-session-id="store.sessionId"
        :busy="store.isSwitchingAgent || store.isLoadingSession"
        :can-create="Boolean(store.agentId)"
        @create="createSession"
        @select="loadSession"
        @delete="requestDeleteSession"
      />

      <section class="conversation-column">
        <RunActivityBar :lifecycle="store.runLifecycle" :notice="store.statusNotice" :events="store.events" :error="store.error" />
        <p v-if="store.error && store.runLifecycle !== 'failed'" class="workspace-alert" role="alert">{{ store.error }}</p>
        <ChatCanvas
          :messages="store.messages"
          :lifecycle="store.runLifecycle"
          :can-submit="store.canSubmit"
          :can-resume="store.canResume"
          :interrupt-summary="interruptSummary"
          :has-agent="Boolean(store.agentId)"
          :switching-agent="store.isSwitchingAgent || store.isLoadingSession"
          :submit-message="submitMessage"
          :resume-run="resumeRun"
          :cancel-run="cancelRun"
          @create-agent="openAgentManager"
        />
      </section>
    </div>

    <AgentManager :open="agentManagerOpen" :initial-template="initialAgentTemplate" @close="closeAgentManager" />

    <AccessibleDialog v-if="deleteTarget" title-id="delete-session-title" :busy="deleteBusy" @close="deleteTarget = null">
      <div class="confirmation-dialog">
        <button class="dialog-close" type="button" aria-label="关闭" @click="deleteTarget = null"><X :size="18" /></button>
        <span class="dialog-symbol danger"><AlertTriangle :size="21" /></span>
        <h2 id="delete-session-title">删除这个会话？</h2>
        <p>“{{ deleteTarget.title || '未命名会话' }}”的消息、运行记录和关联数据会被永久删除。</p>
        <p v-if="store.lastErrorCode === 'SESSION_HAS_ACTIVE_EXECUTION'" class="form-feedback error" role="alert">{{ store.error }}</p>
        <div class="dialog-actions">
          <button type="button" @click="deleteTarget = null">取消</button>
          <button class="danger-button" type="button" :disabled="deleteBusy" @click="deleteSession">
            <LoaderCircle v-if="deleteBusy" :size="16" class="spin" /><Trash2 v-else :size="16" />确认删除
          </button>
        </div>
      </div>
    </AccessibleDialog>

    <AccessibleDialog v-if="changePasswordOpen" title-id="change-password-title" :busy="passwordBusy" @close="changePasswordOpen = false">
      <form class="password-dialog" @submit.prevent="changePassword">
        <button class="dialog-close" type="button" aria-label="关闭" @click="changePasswordOpen = false"><X :size="18" /></button>
        <span class="dialog-symbol"><KeyRound :size="21" /></span>
        <h2 id="change-password-title">修改密码</h2>
        <p>更新后，其他已登录设备会自动退出。</p>
        <label class="field-block"><span>当前密码</span><input v-model="currentPassword" type="password" autocomplete="current-password" required /></label>
        <label class="field-block"><span>新密码</span><input v-model="newPassword" type="password" autocomplete="new-password" minlength="10" required /></label>
        <label class="field-block"><span>确认新密码</span><input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="10" required /></label>
        <p v-if="passwordError" class="form-feedback error" role="alert">{{ passwordError }}</p>
        <p v-if="passwordNotice" class="form-feedback success" role="status">{{ passwordNotice }}</p>
        <div class="dialog-actions"><button type="button" @click="changePasswordOpen = false">取消</button><button class="primary-action" type="submit" :disabled="passwordBusy"><LoaderCircle v-if="passwordBusy" :size="16" class="spin" />保存新密码</button></div>
      </form>
    </AccessibleDialog>
  </main>
</template>
