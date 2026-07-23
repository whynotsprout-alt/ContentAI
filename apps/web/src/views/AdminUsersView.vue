<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import {
  ArrowLeft,
  Ban,
  CheckCircle2,
  Copy,
  FileText,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  UserCheck,
  Users,
  X
} from '@lucide/vue';
import { ApiError, adminApi, type AdminSessionDetail, type AdminSessionSummary, type AdminUsageBucket, type AdminUser } from '../services/api';
import { useAuthStore } from '../stores/auth';
import AccessibleDialog from '../components/AccessibleDialog.vue';
import AdminShell from '../components/AdminShell.vue';
import { createAdminRequestGenerationGuard } from './adminRequestGeneration';

const auth = useAuthStore();
const users = ref<AdminUser[]>([]);
const selected = ref<AdminUser | null>(null);
const search = ref('');
const status = ref('');
const cursorHistory = ref<string[]>([]);
const nextCursor = ref<string | null>(null);
const loading = ref(false);
const detailLoading = ref(false);
const actionLoading = ref(false);
const listError = ref('');
const detailError = ref('');
const detailErrorKind = ref<'load' | 'action' | ''>('');
const auditError = ref('');
const auditRetryKind = ref<'session' | 'sessions' | 'messages' | ''>('');
const notice = ref('');
const temporaryPassword = ref<{ value: string; expiresAt: string; userId: string; userEmail: string } | null>(null);
const passwordRequestPending = ref(false);
const copyFeedback = ref<{ message: string; error: boolean } | null>(null);
const userDetailOpen = ref(false);
const userListPanel = ref<HTMLElement | null>(null);
const userSearchInput = ref<HTMLInputElement | null>(null);
const detailBackButton = ref<HTMLButtonElement | null>(null);
const resetPasswordButton = ref<HTMLButtonElement | null>(null);
const sessions = ref<AdminSessionSummary[]>([]);
const sessionsNextCursor = ref<string | null>(null);
const selectedSession = ref<AdminSessionDetail | null>(null);
const selectedSessionId = ref<string | null>(null);
const messagesNextCursor = ref<string | null>(null);
const auditOpen = ref(false);
const auditDetailOpen = ref(false);
const sessionListScrollTop = ref(0);
const sessionListPanel = ref<HTMLElement | null>(null);
const auditBackButton = ref<HTMLButtonElement | null>(null);
const sessionLoading = ref(false);
const auditPaginationLoading = ref(false);
const usage = ref<AdminUsageBucket[]>([]);
const confirmStatusChange = ref(false);
const confirmRoleChange = ref(false);
const userRequestGuard = createAdminRequestGenerationGuard();
const sessionRequestGuard = createAdminRequestGenerationGuard();
const auditPaginationRequestGuard = createAdminRequestGenerationGuard();
const passwordRequestGuard = createAdminRequestGenerationGuard();
let searchTimer = 0;

const pageNumber = computed(() => cursorHistory.value.length + 1);
const currentCursor = computed(() => cursorHistory.value[cursorHistory.value.length - 1] ?? '');
const canResetPassword = computed(() => Boolean(selected.value) && selected.value?.status !== 'disabled' && selected.value?.id !== auth.user?.id);
const canChangeRole = computed(() => Boolean(selected.value) && !(
  selected.value?.id === auth.user?.id && selected.value?.role === 'admin'
));

function isCompactAdmin() {
  return window.matchMedia('(max-width: 1279px)').matches;
}

function parseError(value: unknown) {
  if (value instanceof ApiError) return `${value.message}${value.requestId ? `（请求标识：${value.requestId}）` : ''}`;
  return value instanceof Error ? value.message : String(value || '请求失败');
}

async function load() {
  loading.value = true;
  listError.value = '';
  try {
    const result = await adminApi.users({ search: search.value.trim(), status: status.value, cursor: currentCursor.value, limit: 50 });
    users.value = result.items;
    nextCursor.value = result.next_cursor;
    if (selected.value) selected.value = users.value.find((user) => user.id === selected.value?.id) ?? selected.value;
  } catch (value) {
    listError.value = parseError(value);
  } finally {
    loading.value = false;
  }
}

async function selectUser(user: AdminUser) {
  const previousUserId = selected.value?.id ?? null;
  const discardedPassword = passwordRequestPending.value || Boolean(temporaryPassword.value);
  passwordRequestGuard.invalidate();
  if (passwordRequestPending.value) actionLoading.value = false;
  passwordRequestPending.value = false;
  closeTemporaryPassword();
  closeAudit();

  const request = userRequestGuard.begin();
  const userId = user.id;
  selected.value = user;
  userDetailOpen.value = true;
  notice.value = discardedPassword
    ? previousUserId && previousUserId !== userId
      ? '上一位用户的临时密码请求已取消，未显示任何密码。'
      : '临时密码请求已取消，未显示任何密码。'
    : '';
  detailError.value = '';
  detailErrorKind.value = '';
  detailLoading.value = true;
  sessions.value = [];
  sessionsNextCursor.value = null;
  usage.value = [];
  selectedSession.value = null;
  selectedSessionId.value = null;
  messagesNextCursor.value = null;
  if (isCompactAdmin()) {
    await nextTick();
    detailBackButton.value?.focus({ preventScroll: true });
  }
  try {
    const [detail, sessionResult, usageResult] = await Promise.all([
      adminApi.user(userId),
      adminApi.sessions(userId),
      adminApi.usage({ user_id: userId })
    ]);
    if (!userRequestGuard.isCurrent(request) || selected.value?.id !== userId) return;
    selected.value = detail;
    sessions.value = sessionResult.items;
    sessionsNextCursor.value = sessionResult.next_cursor;
    usage.value = usageResult.items;
  } catch (value) {
    if (!userRequestGuard.isCurrent(request) || selected.value?.id !== userId) return;
    detailError.value = parseError(value);
    detailErrorKind.value = 'load';
  } finally {
    if (userRequestGuard.isCurrent(request) && selected.value?.id === userId) {
      detailLoading.value = false;
    }
  }
}

function retrySelectedUser() {
  if (!selected.value || detailLoading.value) return;
  void selectUser(selected.value);
}

async function focusUserListTarget() {
  await nextTick();
  const selectedRow = userListPanel.value?.querySelector<HTMLElement>('[aria-selected="true"]');
  (selectedRow ?? userSearchInput.value ?? userListPanel.value)?.focus({ preventScroll: true });
}

function returnToUserList() {
  userRequestGuard.invalidate();
  detailLoading.value = false;
  passwordRequestGuard.invalidate();
  if (passwordRequestPending.value) actionLoading.value = false;
  passwordRequestPending.value = false;
  userDetailOpen.value = false;
  closeTemporaryPassword();
  closeAudit();
  void focusUserListTarget();
}

function onUserRowKeydown(event: KeyboardEvent, user: AdminUser) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  void selectUser(user);
}

async function selectSession(session: AdminSessionSummary) {
  if (!selected.value || !auditOpen.value) return;
  auditPaginationRequestGuard.invalidate();
  auditPaginationLoading.value = false;
  const request = sessionRequestGuard.begin();
  const userId = selected.value.id;
  const sessionId = session.session_id;
  auditError.value = '';
  auditRetryKind.value = '';
  selectedSessionId.value = sessionId;
  if (isCompactAdmin() && !auditDetailOpen.value) {
    sessionListScrollTop.value = sessionListPanel.value?.scrollTop ?? sessionListScrollTop.value;
    auditDetailOpen.value = true;
    await nextTick();
    auditBackButton.value?.focus({ preventScroll: true });
  }
  selectedSession.value = null;
  sessionLoading.value = true;
  try {
    const [detail, messages] = await Promise.all([
      adminApi.sessionDetail(sessionId),
      adminApi.sessionMessages(sessionId)
    ]);
    if (
      !sessionRequestGuard.isCurrent(request)
      || !auditOpen.value
      || selected.value?.id !== userId
      || selectedSessionId.value !== sessionId
    ) return;
    selectedSession.value = { ...detail, messages: messages.items };
    messagesNextCursor.value = messages.next_cursor;
  } catch (value) {
    if (
      !sessionRequestGuard.isCurrent(request)
      || !auditOpen.value
      || selected.value?.id !== userId
      || selectedSessionId.value !== sessionId
    ) return;
    auditError.value = parseError(value);
    auditRetryKind.value = 'session';
  } finally {
    if (
      sessionRequestGuard.isCurrent(request)
      && auditOpen.value
      && selected.value?.id === userId
      && selectedSessionId.value === sessionId
    ) {
      sessionLoading.value = false;
    }
  }
}

function openSessionAudit() {
  if (!sessions.value.length) return;
  auditOpen.value = true;
  auditDetailOpen.value = false;
  auditError.value = '';
  auditRetryKind.value = '';
  if (!isCompactAdmin()) {
    const current = sessions.value.find((session) => session.session_id === selectedSessionId.value) ?? sessions.value[0];
    void selectSession(current);
  }
}

async function returnToAuditList() {
  auditDetailOpen.value = false;
  await nextTick();
  if (sessionListPanel.value) sessionListPanel.value.scrollTop = sessionListScrollTop.value;
  const selectedButton = sessionListPanel.value?.querySelector<HTMLElement>(
    selectedSessionId.value ? `[data-session-id="${selectedSessionId.value}"]` : '[data-session-id]'
  );
  (selectedButton ?? sessionListPanel.value?.querySelector<HTMLElement>('[data-session-id]') ?? sessionListPanel.value)
    ?.focus({ preventScroll: true });
}

function closeAudit() {
  sessionRequestGuard.invalidate();
  auditPaginationRequestGuard.invalidate();
  auditOpen.value = false;
  auditDetailOpen.value = false;
  sessionLoading.value = false;
  auditPaginationLoading.value = false;
  auditError.value = '';
  auditRetryKind.value = '';
}

async function toggleUser() {
  if (!selected.value) return;
  actionLoading.value = true;
  detailError.value = '';
  detailErrorKind.value = '';
  notice.value = '';
  try {
    selected.value = selected.value.status === 'disabled'
      ? await adminApi.enable(selected.value.id)
      : await adminApi.disable(selected.value.id);
    notice.value = selected.value.status === 'disabled' ? '用户已禁用。' : '用户已启用。';
    confirmStatusChange.value = false;
    await load();
  } catch (value) {
    detailError.value = parseError(value);
    detailErrorKind.value = 'action';
  } finally {
    actionLoading.value = false;
  }
}

async function sendReset() {
  if (!selected.value || !canResetPassword.value) return;
  const userId = selected.value.id;
  const userEmail = selected.value.email;
  const request = passwordRequestGuard.begin();
  passwordRequestPending.value = true;
  actionLoading.value = true;
  detailError.value = '';
  detailErrorKind.value = '';
  notice.value = '';
  closeTemporaryPassword();
  try {
    const result = await adminApi.temporaryPassword(userId);
    if (
      !passwordRequestGuard.isCurrent(request)
      || selected.value?.id !== userId
      || !userDetailOpen.value
    ) return;
    actionLoading.value = false;
    await nextTick();
    if (
      !passwordRequestGuard.isCurrent(request)
      || selected.value?.id !== userId
      || !userDetailOpen.value
    ) return;
    resetPasswordButton.value?.focus({ preventScroll: true });
    temporaryPassword.value = {
      value: result.temporary_password,
      expiresAt: result.expires_at,
      userId,
      userEmail
    };
  } catch (value) {
    if (!passwordRequestGuard.isCurrent(request) || selected.value?.id !== userId) return;
    detailError.value = parseError(value);
    detailErrorKind.value = 'action';
  } finally {
    if (passwordRequestGuard.isCurrent(request) && selected.value?.id === userId) {
      passwordRequestPending.value = false;
      actionLoading.value = false;
    }
  }
}

async function loadMoreSessions() {
  if (!selected.value || !sessionsNextCursor.value || auditPaginationLoading.value) return;
  const userId = selected.value.id;
  const request = auditPaginationRequestGuard.begin();
  auditPaginationLoading.value = true;
  auditError.value = '';
  auditRetryKind.value = '';
  try {
    const result = await adminApi.sessions(userId, sessionsNextCursor.value);
    if (
      !auditPaginationRequestGuard.isCurrent(request)
      || !auditOpen.value
      || selected.value?.id !== userId
    ) return;
    sessions.value.push(...result.items);
    sessionsNextCursor.value = result.next_cursor;
  } catch (value) {
    if (
      !auditPaginationRequestGuard.isCurrent(request)
      || !auditOpen.value
      || selected.value?.id !== userId
    ) return;
    auditError.value = parseError(value);
    auditRetryKind.value = 'sessions';
  } finally {
    if (
      auditPaginationRequestGuard.isCurrent(request)
      && auditOpen.value
      && selected.value?.id === userId
    ) {
      auditPaginationLoading.value = false;
    }
  }
}

function closeTemporaryPassword() {
  temporaryPassword.value = null;
  copyFeedback.value = null;
}

async function copyTemporaryPassword() {
  if (!temporaryPassword.value) return;
  copyFeedback.value = null;
  try {
    await navigator.clipboard.writeText(temporaryPassword.value.value);
    copyFeedback.value = { message: '临时密码已复制。', error: false };
  } catch {
    copyFeedback.value = { message: '复制失败，请手动选择密码后复制。', error: true };
  }
}

async function loadMoreMessages() {
  if (!selectedSession.value || !messagesNextCursor.value || auditPaginationLoading.value) return;
  const sessionId = selectedSession.value.session_id;
  const request = auditPaginationRequestGuard.begin();
  auditPaginationLoading.value = true;
  auditError.value = '';
  auditRetryKind.value = '';
  try {
    const result = await adminApi.sessionMessages(sessionId, messagesNextCursor.value);
    if (
      !auditPaginationRequestGuard.isCurrent(request)
      || !auditOpen.value
      || selectedSession.value?.session_id !== sessionId
    ) return;
    selectedSession.value.messages.push(...result.items);
    messagesNextCursor.value = result.next_cursor;
  } catch (value) {
    if (
      !auditPaginationRequestGuard.isCurrent(request)
      || !auditOpen.value
      || selectedSession.value?.session_id !== sessionId
    ) return;
    auditError.value = parseError(value);
    auditRetryKind.value = 'messages';
  } finally {
    if (
      auditPaginationRequestGuard.isCurrent(request)
      && auditOpen.value
      && selectedSession.value?.session_id === sessionId
    ) {
      auditPaginationLoading.value = false;
    }
  }
}

function retryAuditRequest() {
  if (auditRetryKind.value === 'sessions') {
    void loadMoreSessions();
    return;
  }
  if (auditRetryKind.value === 'messages') {
    void loadMoreMessages();
    return;
  }
  if (auditRetryKind.value === 'session' && selectedSessionId.value) {
    const session = sessions.value.find((item) => item.session_id === selectedSessionId.value);
    if (session) void selectSession(session);
  }
}

async function nextPage() {
  if (!nextCursor.value || loading.value) return;
  cursorHistory.value.push(nextCursor.value);
  await load();
}

async function previousPage() {
  if (!cursorHistory.value.length || loading.value) return;
  cursorHistory.value.pop();
  await load();
}

function resetPagination() {
  cursorHistory.value = [];
  nextCursor.value = null;
}

async function changeRole() {
  if (!selected.value || !canChangeRole.value) return;
  actionLoading.value = true;
  detailError.value = '';
  detailErrorKind.value = '';
  notice.value = '';
  try {
    const nextRole = selected.value.role === 'admin' ? 'user' : 'admin';
    selected.value = await adminApi.updateUser(selected.value.id, nextRole);
    notice.value = nextRole === 'admin' ? '用户已设为管理员。' : '管理员已降级为普通用户。';
    confirmRoleChange.value = false;
    await load();
  } catch (value) {
    detailError.value = parseError(value);
    detailErrorKind.value = 'action';
  } finally {
    actionLoading.value = false;
  }
}

function statusText(value: string) {
  if (value === 'active') return '正常';
  if (value === 'disabled') return '已禁用';
  return '待验证';
}

function formatTokens(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value);
}

function formatDate(value: string | null) {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '无';
}

watch(search, () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => { resetPagination(); void load(); }, 250);
});
watch(status, () => { resetPagination(); void load(); });
onMounted(() => void load());
</script>

<template>
  <AdminShell title="用户" description="查看注册状态、数据规模和模型 Token 消耗。">
      <template #heading-status><div class="admin-total" aria-live="polite"><Users :size="18" /><strong>{{ users.length }}</strong><span>本页用户</span></div></template>
      <div class="admin-workspace" :class="{ 'admin-show-detail': userDetailOpen }">
        <section ref="userListPanel" class="admin-list-panel" aria-label="用户列表" :aria-busy="loading" tabindex="-1">
          <div class="admin-filters"><label class="compact-search"><Search :size="16" /><input ref="userSearchInput" v-model="search" type="search" placeholder="搜索邮箱" aria-label="搜索用户邮箱" /></label><select v-model="status" aria-label="筛选用户状态"><option value="">全部状态</option><option value="active">正常</option><option value="pending_verification">待验证</option><option value="disabled">已禁用</option></select><button class="icon-action" type="button" aria-label="刷新用户列表" :disabled="loading" @click="load"><RefreshCw :size="16" :class="{ spin: loading }" /></button></div>
          <p v-if="listError" class="workspace-alert" role="alert">{{ listError }}</p>
          <div v-if="loading" class="admin-skeleton" aria-label="用户列表加载中"><span v-for="i in 6" :key="i"></span></div>
          <div v-else-if="!users.length" class="admin-empty"><Users :size="28" /><strong>暂无匹配用户</strong><span>调整搜索词或状态筛选后重试。</span></div>
          <div v-else class="admin-table-wrap">
            <table class="admin-table"><caption class="sr-only">ContentAI 用户及 Token 用量</caption><thead><tr><th>用户</th><th>状态</th><th>内容账号</th><th>会话</th><th>输入 Token</th><th>输出 Token</th><th>总 Token</th></tr></thead><tbody>
              <tr v-for="user in users" :key="user.id" :data-user-id="user.id" tabindex="0" :aria-selected="selected?.id === user.id" :class="{ selected: selected?.id === user.id }" @click="selectUser(user)" @keydown="onUserRowKeydown($event, user)">
                <td data-label="用户"><strong>{{ user.email }}</strong><small>{{ user.role === 'admin' ? '管理员' : '普通用户' }}</small></td>
                <td data-label="状态"><span class="semantic-status" :class="user.status">{{ statusText(user.status) }}</span></td><td data-label="内容账号">{{ user.agent_count }}</td><td data-label="会话">{{ user.conversation_count }}</td><td data-label="输入 Token" class="token-number">{{ formatTokens(user.input_tokens) }}</td><td data-label="输出 Token" class="token-number">{{ formatTokens(user.output_tokens) }}</td><td data-label="总 Token" class="token-number strong">{{ formatTokens(user.total_tokens) }}</td>
              </tr>
            </tbody></table>
          </div>
          <footer class="admin-pagination"><span>第 {{ pageNumber }} 页</span><div><button :disabled="!cursorHistory.length || loading" @click="previousPage">上一页</button><button :disabled="!nextCursor || loading" @click="nextPage">下一页</button></div></footer>
        </section>

        <aside class="admin-detail-panel" aria-label="用户详情" aria-live="polite" :aria-busy="detailLoading">
          <div v-if="!selected" class="admin-empty"><Users :size="28" /><strong>选择一位用户</strong><span>详细状态和管理操作会显示在这里。</span></div>
          <template v-else>
            <button ref="detailBackButton" class="admin-compact-back" type="button" @click="returnToUserList"><ArrowLeft :size="17" />返回用户列表</button>
            <div v-if="detailError" class="workspace-alert admin-detail-alert" role="alert"><span>{{ detailError }}</span><button v-if="detailErrorKind === 'load'" type="button" :disabled="detailLoading" @click="retrySelectedUser"><RefreshCw :size="15" />重新加载用户详情</button></div>
            <header class="admin-detail-header"><span class="detail-avatar">{{ selected.email.slice(0, 1).toUpperCase() }}</span><div><h2>{{ selected.email }}</h2><p>{{ selected.role === 'admin' ? '管理员' : '普通用户' }}</p></div></header>
            <dl class="admin-detail-list"><div><dt>状态</dt><dd>{{ statusText(selected.status) }}</dd></div><div><dt>邮箱验证</dt><dd>{{ selected.email_verified_at ? '已验证' : '未验证' }}</dd></div><div><dt>密码</dt><dd>{{ selected.password_set ? '已设置，无法查看原文' : '未设置' }}</dd></div><div><dt>注册时间</dt><dd>{{ formatDate(selected.created_at) }}</dd></div><div><dt>最近登录</dt><dd>{{ formatDate(selected.last_login_at) }}</dd></div><div><dt>统计完整度</dt><dd>{{ Math.round(selected.usage_coverage * 100) }}% <small v-if="selected.missing_usage_call_count">缺失 {{ selected.missing_usage_call_count }} 次</small></dd></div></dl>
            <section class="detail-token-grid"><div><span>输入</span><strong>{{ formatTokens(selected.input_tokens) }}</strong></div><div><span>输出</span><strong>{{ formatTokens(selected.output_tokens) }}</strong></div><div><span>总量</span><strong>{{ formatTokens(selected.total_tokens) }}</strong></div></section>
            <section class="admin-detail-section"><header><FileText :size="16" /><strong>会话审计</strong><span>{{ sessions.length }} 条</span></header><button class="admin-open-audit" type="button" :disabled="!sessions.length" @click="openSessionAudit"><span>查看会话记录</span><small>{{ sessions.length ? '仅显示用户与 Agent 对话' : '该用户暂无会话' }}</small></button></section>
            <section v-if="usage.length" class="admin-detail-section"><header><strong>近期用量</strong></header><p v-for="bucket in usage.slice(0, 7)" :key="bucket.bucket" class="admin-usage-row"><span>{{ bucket.bucket }}</span><strong>{{ formatTokens(bucket.total_tokens) }} Token</strong></p></section>
            <p v-if="notice" class="form-feedback success" role="status"><CheckCircle2 :size="16" /> {{ notice }}</p>
            <div class="admin-actions">
              <button ref="resetPasswordButton" type="button" :disabled="actionLoading || !canResetPassword" :title="canResetPassword ? '生成一次性临时密码' : selected.id === auth.user?.id ? '不能为当前管理员生成临时密码' : '已禁用用户不可重置密码'" @click="sendReset"><LoaderCircle v-if="actionLoading" :size="16" class="spin" /><KeyRound v-else :size="16" />生成临时密码</button>
              <button type="button" :disabled="actionLoading || !canChangeRole" :title="canChangeRole ? '修改用户角色' : '不能降级当前登录的管理员'" @click="confirmRoleChange = true"><ShieldCheck :size="16" />{{ selected.role === 'admin' ? '降级为普通用户' : '提升为管理员' }}</button>
              <button class="danger-button" type="button" :disabled="actionLoading || selected.id === auth.user?.id" @click="confirmStatusChange = true"><Ban v-if="selected.status !== 'disabled'" :size="16" /><UserCheck v-else :size="16" />{{ selected.status === 'disabled' ? '启用用户' : '禁用用户' }}</button>
            </div>
          </template>
        </aside>
      </div>
    <AccessibleDialog v-if="confirmStatusChange && selected" title-id="status-confirm-title" :busy="actionLoading" @close="confirmStatusChange = false">
      <div class="confirmation-dialog"><button class="dialog-close" type="button" aria-label="关闭" @click="confirmStatusChange = false"><X :size="18" /></button><span class="dialog-symbol danger"><Ban :size="21" /></span><h2 id="status-confirm-title">{{ selected.status === 'disabled' ? '启用这个用户？' : '禁用这个用户？' }}</h2><p>{{ selected.status === 'disabled' ? '启用后，用户可以重新登录和使用工作台。' : '禁用后，用户会失去访问权限，正在使用的身份也会失效。' }}</p><div class="dialog-actions"><button type="button" @click="confirmStatusChange = false">取消</button><button class="danger-button" type="button" :disabled="actionLoading" @click="toggleUser"><LoaderCircle v-if="actionLoading" :size="16" class="spin" />确认{{ selected.status === 'disabled' ? '启用' : '禁用' }}</button></div></div>
    </AccessibleDialog>

    <AccessibleDialog v-if="confirmRoleChange && selected" title-id="role-confirm-title" :busy="actionLoading" @close="confirmRoleChange = false">
      <div class="confirmation-dialog"><button class="dialog-close" type="button" aria-label="关闭" @click="confirmRoleChange = false"><X :size="18" /></button><span class="dialog-symbol"><ShieldCheck :size="21" /></span><h2 id="role-confirm-title">{{ selected.role === 'admin' ? '降级这个管理员？' : '提升这个用户为管理员？' }}</h2><p>{{ selected.role === 'admin' ? '降级后将失去管理后台权限；系统禁止降级最后一个可用管理员。' : '管理员可以查看用户、会话审计和模型用量，并管理其他用户角色。' }}</p><div class="dialog-actions"><button type="button" @click="confirmRoleChange = false">取消</button><button type="button" :disabled="actionLoading" @click="changeRole"><LoaderCircle v-if="actionLoading" :size="16" class="spin" />确认修改角色</button></div></div>
    </AccessibleDialog>

    <AccessibleDialog v-if="temporaryPassword" title-id="temporary-password-title" @close="closeTemporaryPassword">
      <section class="admin-temporary-password-dialog">
        <button class="dialog-close" type="button" aria-label="关闭临时密码" @click="closeTemporaryPassword"><X :size="18" /></button>
        <span class="dialog-symbol"><KeyRound :size="21" /></span>
        <h2 id="temporary-password-title">临时密码（仅显示一次）</h2>
        <p>这是为 <strong>{{ temporaryPassword.userEmail }}</strong> 生成的临时密码。请立即通过安全渠道交给该用户；关闭窗口后，密码会从本页面清除且无法再次查看。</p>
        <div class="admin-temporary-password">
          <code>{{ temporaryPassword.value }}</code>
          <small>有效期至 {{ formatDate(temporaryPassword.expiresAt) }}</small>
        </div>
        <button class="admin-copy-password" type="button" @click="copyTemporaryPassword"><Copy :size="16" />复制临时密码</button>
        <p v-if="copyFeedback" class="form-feedback" :class="copyFeedback.error ? 'error' : 'success'" :role="copyFeedback.error ? 'alert' : 'status'">{{ copyFeedback.message }}</p>
        <div class="dialog-actions"><button type="button" @click="closeTemporaryPassword">关闭并清除</button></div>
      </section>
    </AccessibleDialog>

    <AccessibleDialog v-if="auditOpen" title-id="audit-window-title" variant="fullscreen" @close="closeAudit">
      <section class="admin-audit-window">
        <header class="admin-audit-header"><div><span class="section-kicker"><FileText :size="15" /> 会话审计</span><h2 id="audit-window-title">{{ selected?.email }} 的会话记录</h2></div><button class="icon-action" type="button" aria-label="关闭会话审计" @click="closeAudit"><X :size="19" /></button></header>
        <div class="admin-audit-body" :class="{ 'audit-show-transcript': auditDetailOpen }"><aside ref="sessionListPanel" class="admin-audit-sessions" aria-label="会话索引" tabindex="-1"><header><strong>会话</strong><span>已加载 {{ sessions.length }} 条</span></header><div v-if="auditError && auditRetryKind === 'sessions'" class="workspace-alert admin-audit-alert" role="alert"><span>{{ auditError }}</span><button type="button" @click="retryAuditRequest"><RefreshCw :size="15" />重试加载会话列表</button></div><button v-for="session in sessions" :key="session.session_id" type="button" :data-session-id="session.session_id" :class="{ selected: selectedSessionId === session.session_id }" :aria-current="selectedSessionId === session.session_id ? 'page' : undefined" @click="selectSession(session)"><strong>{{ session.title || '未命名会话' }}</strong><small>{{ session.message_count }} 条消息 · {{ formatDate(session.updated_at) }}</small></button><button v-if="sessionsNextCursor" class="admin-load-more" type="button" :disabled="auditPaginationLoading" @click="loadMoreSessions"><LoaderCircle v-if="auditPaginationLoading" :size="15" class="spin" />加载更多会话</button></aside>
          <section class="admin-transcript" :class="{ 'admin-transcript-has-error': auditError && auditRetryKind !== 'sessions' }" aria-label="只读会话记录" :aria-busy="sessionLoading || auditPaginationLoading"><header><button ref="auditBackButton" class="admin-audit-back" type="button" @click="returnToAuditList"><ArrowLeft :size="17" />返回会话列表</button><div v-if="selectedSession" class="admin-transcript-heading"><div><span>会话记录</span><h3>{{ selectedSession.title }}</h3></div><small>已加载 {{ selectedSession.messages.length }} 条消息</small></div></header><div v-if="auditError && auditRetryKind !== 'sessions'" class="workspace-alert admin-audit-alert" role="alert"><span>{{ auditError }}</span><button type="button" @click="retryAuditRequest"><RefreshCw :size="15" />{{ auditRetryKind === 'messages' ? '重试加载消息' : '重试加载会话' }}</button></div><div v-if="sessionLoading" class="editor-loading" role="status"><LoaderCircle :size="20" class="spin" />正在加载会话</div><div v-else-if="selectedSession" class="admin-session-transcript"><article v-for="message in selectedSession.messages" :key="String(message.id)" :class="String(message.role)"><strong>{{ message.role === 'user' ? '用户' : 'Agent' }}</strong><p>{{ String(message.content || '') }}</p></article><p v-if="!selectedSession.messages.length" class="admin-empty">该会话暂无可显示的对话记录。</p><button v-if="messagesNextCursor" class="admin-load-more" type="button" :disabled="auditPaginationLoading" @click="loadMoreMessages"><LoaderCircle v-if="auditPaginationLoading" :size="15" class="spin" />加载更多消息</button></div><div v-else-if="!sessionLoading && !auditError" class="admin-empty"><FileText :size="28" /><strong>选择一个会话</strong><span>这里只展示用户与 Agent 的对话记录。</span></div></section>
        </div>
      </section>
    </AccessibleDialog>
  </AdminShell>
</template>
