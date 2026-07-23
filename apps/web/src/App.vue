<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { AlertTriangle, LoaderCircle, Trash2, X } from '@lucide/vue';
import { useRouter } from 'vue-router';
import type { ChatSessionSummary, ResumeDecision } from './services/api';
import { refreshAfterPasswordChange } from './auth/foundation';
import { useAuthStore } from './stores/auth';
import { useWorkbenchStore } from './stores/workbench';
import AccessibleDialog from './components/AccessibleDialog.vue';
import AgentManager from './components/AgentManager.vue';
import ChatCanvas from './components/ChatCanvas.vue';
import ChangePasswordForm from './components/ChangePasswordForm.vue';
import RunActivityBar from './components/RunActivityBar.vue';
import SessionRail from './components/SessionRail.vue';
import WorkbenchHeader from './components/WorkbenchHeader.vue';

const router = useRouter();
const auth = useAuthStore();
const store = useWorkbenchStore();

const SESSION_RAIL_EXPANDED_KEY = 'contentai:session-rail-expanded';
const agentManagerOpen = ref(false);
const initialAgentTemplate = ref<'finance' | 'ai' | null>(null);
const deleteTarget = ref<ChatSessionSummary | null>(null);
const deleteBusy = ref(false);
const changePasswordOpen = ref(false);
const passwordBusy = ref(false);
const isMobileViewport = ref(false);
const isTabletViewport = ref(false);
const mobileSessionNavigationOpen = ref(false);
const tabletSessionNavigationExpanded = ref(false);
let agentManagerReturnFocus: HTMLElement | null = null;
let sessionNavigationReturnFocus: HTMLElement | null = null;
let mobileViewportQuery: MediaQueryList | null = null;
let tabletViewportQuery: MediaQueryList | null = null;

const sessionNavigationExpanded = computed(() => {
  if (isMobileViewport.value) return mobileSessionNavigationOpen.value;
  if (isTabletViewport.value) return tabletSessionNavigationExpanded.value;
  return true;
});

const sessionNavigationCompact = computed(() =>
  isTabletViewport.value && !tabletSessionNavigationExpanded.value
);

function syncNavigationViewport() {
  isMobileViewport.value = Boolean(mobileViewportQuery?.matches);
  isTabletViewport.value = Boolean(tabletViewportQuery?.matches);
  if (!isMobileViewport.value) {
    mobileSessionNavigationOpen.value = false;
    sessionNavigationReturnFocus = null;
  }
}

function focusSessionNavigation() {
  const navigation = document.getElementById('session-navigation');
  navigation?.querySelector<HTMLElement>('[data-session-drawer-close], button, input')?.focus();
}

function toggleSessionNavigation() {
  if (isMobileViewport.value) {
    if (mobileSessionNavigationOpen.value) {
      closeSessionNavigation();
      return;
    }
    sessionNavigationReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    mobileSessionNavigationOpen.value = true;
    void nextTick(focusSessionNavigation);
    return;
  }
  if (isTabletViewport.value) {
    tabletSessionNavigationExpanded.value = !tabletSessionNavigationExpanded.value;
    localStorage.setItem(SESSION_RAIL_EXPANDED_KEY, String(tabletSessionNavigationExpanded.value));
  }
}

function closeSessionNavigation() {
  if (!isMobileViewport.value || !mobileSessionNavigationOpen.value) return;
  mobileSessionNavigationOpen.value = false;
  const target = sessionNavigationReturnFocus;
  sessionNavigationReturnFocus = null;
  void nextTick(() => {
    if (target?.isConnected) target.focus();
  });
}

function onSessionNavigationKeydown(event: KeyboardEvent) {
  if (!isMobileViewport.value || !mobileSessionNavigationOpen.value) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeSessionNavigation();
    return;
  }
  if (event.key !== 'Tab') return;
  const navigation = document.getElementById('session-navigation');
  if (!navigation) return;
  const focusable = Array.from(navigation.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'
  )).filter((element) => element.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}

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
  closeSessionNavigation();
}

async function loadSession(sessionId: string) {
  await store.loadSession(sessionId);
  closeSessionNavigation();
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

async function resumeRun(decision: ResumeDecision) {
  return store.resume(decision);
}

async function cancelRun() {
  await store.cancelActiveRun();
}

function openPasswordDialog() {
  changePasswordOpen.value = true;
}

async function passwordChanged() {
  try {
    await refreshAfterPasswordChange({ refresh: () => auth.refresh(), clear: () => auth.clear() });
    changePasswordOpen.value = false;
  } catch {
    await router.replace({ path: '/login', query: { notice: 'password-changed' } });
  }
}

async function logout() {
  await auth.logout();
  await router.replace('/login');
}

onMounted(() => {
  mobileViewportQuery = window.matchMedia('(max-width: 767px)');
  tabletViewportQuery = window.matchMedia('(min-width: 768px) and (max-width: 1023px)');
  tabletSessionNavigationExpanded.value = localStorage.getItem(SESSION_RAIL_EXPANDED_KEY) === 'true';
  mobileViewportQuery.addEventListener('change', syncNavigationViewport);
  tabletViewportQuery.addEventListener('change', syncNavigationViewport);
  document.addEventListener('keydown', onSessionNavigationKeydown);
  syncNavigationViewport();
  void store.boot().catch((value) => {
    store.error = value instanceof Error ? value.message : String(value);
  });
});

onBeforeUnmount(() => {
  mobileViewportQuery?.removeEventListener('change', syncNavigationViewport);
  tabletViewportQuery?.removeEventListener('change', syncNavigationViewport);
  document.removeEventListener('keydown', onSessionNavigationKeydown);
  store.eventSource?.close();
});
</script>

<template>
  <main class="workbench-shell">
    <a class="skip-link" href="#main-content">跳到对话区</a>

    <WorkbenchHeader
      :inert="isMobileViewport && mobileSessionNavigationOpen ? true : undefined"
      :agents="store.agents"
      :active-agent-id="store.agentId"
      :user-email="auth.user?.email ?? ''"
      :is-admin="auth.isAdmin"
      :switching="store.isSwitchingAgent || store.isLoadingSessions"
      :show-session-navigation-toggle="isMobileViewport || isTabletViewport"
      :session-navigation-expanded="sessionNavigationExpanded"
      @toggle-session-navigation="toggleSessionNavigation"
      @choose-agent="chooseAgent"
      @open-agents="openAgentManager()"
      @open-admin="router.push('/admin/users')"
      @change-password="openPasswordDialog"
      @logout="logout"
    />

    <div class="workbench-grid" :class="{ 'has-open-session-drawer': isMobileViewport && mobileSessionNavigationOpen }">
      <button
        v-if="isMobileViewport && mobileSessionNavigationOpen"
        class="session-rail-scrim"
        type="button"
        aria-label="关闭会话导航"
        @click="closeSessionNavigation"
      ></button>

      <SessionRail
        id="session-navigation"
        :class="{ 'is-open': mobileSessionNavigationOpen, 'is-compact': sessionNavigationCompact }"
        :aria-hidden="isMobileViewport && !mobileSessionNavigationOpen ? 'true' : undefined"
        :inert="isMobileViewport && !mobileSessionNavigationOpen ? true : undefined"
        :sessions="store.sessions"
        :active-session-id="store.sessionId"
        :busy="store.isSwitchingAgent || store.isLoadingSession || store.isLoadingSessions"
        :can-create="Boolean(store.agentId)"
        :has-more="Boolean(store.sessionNextCursor)"
        :loading-more="store.isLoadingSessions"
        :compact="sessionNavigationCompact"
        :drawer="isMobileViewport"
        @close="closeSessionNavigation"
        @create="createSession"
        @load-more="store.loadMoreSessions()"
        @select="loadSession"
        @delete="requestDeleteSession"
      />

      <section
        class="conversation-column"
        :inert="isMobileViewport && mobileSessionNavigationOpen ? true : undefined"
      >
        <RunActivityBar :lifecycle="store.runLifecycle" :notice="store.statusNotice" :events="store.events" :error="store.error" />
        <p v-if="store.error && store.runLifecycle !== 'failed'" class="workspace-alert" role="alert">{{ store.error }}</p>
        <ChatCanvas
          :messages="store.messages"
          :lifecycle="store.runLifecycle"
          :can-submit="store.canSubmit"
          :can-resume="store.canResume"
          :pending-interrupt="store.pendingInterrupt"
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
      <ChangePasswordForm title-id="change-password-title" :on-changed="passwordChanged" @busy="passwordBusy = $event" @close="changePasswordOpen = false" />
    </AccessibleDialog>
  </main>
</template>
