<script setup lang="ts">
import {
  Check,
  ChevronDown,
  History,
  LoaderCircle,
  MessageCirclePlus,
  MessageSquareText,
  Moon,
  Play,
  Plus,
  Save,
  Settings,
  Search,
  Sparkles,
  Sun,
  Trash2,
  X
} from '@lucide/vue';
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  api,
  type AccountDetail,
  type AccountPayload,
} from './services/api';
import { useWorkbenchStore } from './stores/workbench';

const store = useWorkbenchStore();
const defaultPrompt = '直接输入你想讨论或处理的问题';
const prompt = ref('');
const accountMenuOpen = ref(false);
const accountPickerRoot = ref<HTMLElement | null>(null);
const chatStream = ref<HTMLElement | null>(null);
const promptInput = ref<HTMLTextAreaElement | null>(null);
const highlightedAccountIndex = ref(0);
const themeMode = ref<'light' | 'dark'>('dark');
const accountManagerOpen = ref(false);
const accountFormLoading = ref(false);
const accountSaving = ref(false);
const accountFormMode = ref<'create' | 'edit'>('edit');
const accountMessage = ref('');
const accountSearchTerm = ref('');
const accountEditingId = ref('');
const accountForm = ref({
  name: '',
  positioning: '',
  topic_scoring_prompt: '',
  content_creation_prompt: '',
  hotspot_sources: [] as string[]
});
const accountNameInput = ref<HTMLInputElement | null>(null);
const markdownRenderer = new MarkdownIt({
  breaks: true,
  html: false,
  linkify: true
});

const HOTSPOT_SOURCE_OPTIONS = [
  { id: '36kr', label: '36Kr' },
  { id: 'huxiu', label: '虎嗅' },
  { id: 'ifanr', label: '爱范儿' },
  { id: 'douyin', label: '抖音' },
  { id: 'bilibili', label: 'Bilibili' },
  { id: 'xiaohongshu', label: '小红书' },
  { id: 'weibo', label: '微博' },
  { id: 'aihot', label: 'AI HOT' }
];

const DEFAULT_ACCOUNT_FORM: {
  name: string;
  positioning: string;
  topic_scoring_prompt: string;
  content_creation_prompt: string;
  hotspot_sources: string[];
} = {
  name: '',
  positioning: '',
  topic_scoring_prompt: '',
  content_creation_prompt: '',
  hotspot_sources: HOTSPOT_SOURCE_OPTIONS.map((source) => source.id)
};

const filteredAccounts = computed(() => {
  const term = accountSearchTerm.value.trim().toLowerCase();
  if (!term) return store.accounts;
  return store.accounts.filter((account) => {
    const haystack = `${account.name} ${account.positioning} ${account.topic_scoring_prompt} ${account.content_creation_prompt}`.toLowerCase();
    return haystack.includes(term);
  });
});
const canSaveAccount = computed(() => {
  const payload = accountPayload();
  return Boolean(
    payload.name &&
      payload.positioning &&
      payload.topic_scoring_prompt &&
      payload.content_creation_prompt &&
      payload.hotspot_sources.length
  );
});

const selectedAccountName = computed(() => {
  return store.selectedAccount?.name ?? '请选择账号';
});

const isDarkMode = computed(() => themeMode.value === 'dark');

function applyTheme(mode: 'light' | 'dark') {
  themeMode.value = mode;
  document.documentElement.dataset.theme = mode;
  document.documentElement.style.colorScheme = mode;
  window.localStorage.setItem('contentai-theme', mode);
}

function toggleTheme() {
  applyTheme(isDarkMode.value ? 'light' : 'dark');
}

onMounted(() => {
  const savedTheme = window.localStorage.getItem('contentai-theme');
  applyTheme(savedTheme === 'light' ? 'light' : 'dark');
  store.boot().catch((error) => {
    store.error = String(error);
  });
  document.addEventListener('pointerdown', handleDocumentPointerDown);
  nextTick(() => {
    promptInput.value?.focus();
  });
});

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown);
});

function submit() {
  const rawMessage = prompt.value.trim();
  const message = rawMessage || defaultPrompt;
  if (!store.accountId) {
    window.alert('请先创建或选择一个账号后再发送。');
    return;
  }
  if (!store.canSubmit) {
    return;
  }
  store.submit(message);
  prompt.value = '';
  nextTick(() => {
    promptInput.value?.focus();
  });
}

function resetAccountForm() {
  accountEditingId.value = '';
  accountForm.value = {
    ...DEFAULT_ACCOUNT_FORM,
    hotspot_sources: [...DEFAULT_ACCOUNT_FORM.hotspot_sources]
  };
}

function hydrateAccountForm(account: AccountDetail) {
  accountEditingId.value = account.id;
  accountForm.value = {
    name: account.name,
    positioning: account.positioning,
    topic_scoring_prompt: account.topic_scoring_prompt,
    content_creation_prompt: account.content_creation_prompt,
    hotspot_sources: [...account.hotspot_sources]
  };
}

function accountPayload(): AccountPayload {
  return {
    name: accountForm.value.name.trim(),
    positioning: accountForm.value.positioning.trim(),
    topic_scoring_prompt: accountForm.value.topic_scoring_prompt.trim(),
    content_creation_prompt: accountForm.value.content_creation_prompt.trim(),
    hotspot_sources: accountForm.value.hotspot_sources
  };
}

function toggleHotspotSource(sourceId: string) {
  const selected = accountForm.value.hotspot_sources;
  if (selected.includes(sourceId)) {
    accountForm.value.hotspot_sources = selected.filter((item) => item !== sourceId);
    return;
  }
  accountForm.value.hotspot_sources = [...selected, sourceId];
}


function startNewAccount() {
  accountFormMode.value = 'create';
  accountMessage.value = '';
  resetAccountForm();
}

async function editAccount(accountId: string) {
  accountFormMode.value = 'edit';
  accountMessage.value = '';
  accountFormLoading.value = true;
  try {
    const account = await api.account(accountId);
    hydrateAccountForm(account);
  } catch (error) {
    accountMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    accountFormLoading.value = false;
  }
}

function openAccountManager() {
  accountSearchTerm.value = '';
  accountManagerOpen.value = true;
  if (store.accountId) {
    void editAccount(store.accountId).finally(() => {
      nextTick(() => {
        accountNameInput.value?.focus();
      });
    });
    return;
  }
  startNewAccount();
  nextTick(() => {
    accountNameInput.value?.focus();
  });
}

function closeAccountManager() {
  accountManagerOpen.value = false;
  accountMessage.value = '';
  accountSearchTerm.value = '';
}

async function saveAccount() {
  const payload = accountPayload();
  if (!canSaveAccount.value) {
    accountMessage.value = '账号名称、定位、选题评分提示词、内容创作提示词和热点来源不能为空。';
    return;
  }
  accountSaving.value = true;
  accountMessage.value = '';
  try {
    const account =
      accountFormMode.value === 'create'
        ? await store.createAccount(payload)
        : await store.updateAccount(accountEditingId.value, {
            name: payload.name,
            positioning: payload.positioning,
            topic_scoring_prompt: payload.topic_scoring_prompt,
            content_creation_prompt: payload.content_creation_prompt,
            hotspot_sources: payload.hotspot_sources
          });
    accountMessage.value = '账号配置保存成功。';
    accountFormMode.value = 'edit';
    hydrateAccountForm(account);
    closeAccountManager();
  } catch (error) {
    accountMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    accountSaving.value = false;
  }
}

async function deleteCurrentAccount() {
  if (accountFormMode.value !== 'edit' || !accountEditingId.value) return;
  const accountLabel = accountForm.value.name || '当前账号';
  const confirmedOnce = window.confirm(`确认删除账号【${accountLabel}】吗？此操作将执行账号移除。`);
  if (!confirmedOnce) return;
  const confirmedTwice = window.confirm(`请再次确认：确认彻底删除账号【${accountLabel}】吗？删除后不可恢复。`);
  if (!confirmedTwice) return;
  accountSaving.value = true;
  accountMessage.value = '';
  try {
    const deletedId = accountEditingId.value;
    await store.deleteAccount(deletedId);
    accountMessage.value = '账号已成功删除。';
    if (store.accountId) {
      await editAccount(store.accountId);
    } else {
      startNewAccount();
      accountFormMode.value = 'create';
    }
    nextTick(() => {
      accountNameInput.value?.focus();
    });
  } catch (error) {
    accountMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    accountSaving.value = false;
  }
}

function toggleAccountMenu() {
  if (!store.accounts.length) return;
  accountMenuOpen.value = !accountMenuOpen.value;
  highlightedAccountIndex.value = Math.max(
    0,
    store.accounts.findIndex((account) => account.id === store.accountId)
  );
}

function chooseAccount(accountId: string) {
  store.accountId = accountId;
  accountMenuOpen.value = false;
}

function closeAccountMenu() {
  accountMenuOpen.value = false;
}

function handleAccountKeydown(event: KeyboardEvent) {
  if (!store.accounts.length) return;

  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    accountMenuOpen.value = true;
    const direction = event.key === 'ArrowDown' ? 1 : -1;
    highlightedAccountIndex.value =
      (highlightedAccountIndex.value + direction + store.accounts.length) % store.accounts.length;
    return;
  }

  if (event.key === 'Enter' && accountMenuOpen.value) {
    event.preventDefault();
    chooseAccount(store.accounts[highlightedAccountIndex.value].id);
  }
}

function handleDocumentPointerDown(event: PointerEvent) {
  if (!accountPickerRoot.value) return;
  if (!accountPickerRoot.value.contains(event.target as Node)) {
    accountMenuOpen.value = false;
  }
}

function sessionStatusLabel(status: string | null) {
  if (!status) return '未开始';
  const labels: Record<string, string> = {
    idle: '未开始',
    queued: '排队中',
    running: '运行中',
    completed: '已完成',
    cancelled: '已取消',
    interrupted: '已中断',
    failed: '执行失败'
  };
  return labels[status] ?? status;
}

function sessionDisplayTitle(title: string, messageCount: number) {
  if (title && title !== 'New Session') return title;
  return messageCount > 0 ? '未命名会话' : '新会话';
}

function sessionMeta(messageCount: number, status: string | null) {
  const countText = messageCount > 0 ? `${messageCount} 条消息` : '暂无消息';
  return `${countText} · ${sessionStatusLabel(status)}`;
}

function renderMessageContent(message: { content: string; message_type: string }) {
  const html =
    message.message_type === 'markdown'
      ? markdownRenderer.render(message.content)
      : markdownRenderer.utils.escapeHtml(message.content).replace(/\n/g, '<br>');
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true }
  });
}

watch(
  () => store.messages.length,
  () => {
    nextTick(() => {
      if (chatStream.value) {
        chatStream.value.scrollTop = chatStream.value.scrollHeight;
      }
    });
  }
);

</script>

<template>
  <main class="glass-app">
    <div class="scene-texture" aria-hidden="true"></div>

    <section class="hero-panel">
      <div class="hero-copy">
        <div class="brand hero-brand">
          <div class="brand-mark"><Sparkles :size="16" /></div>
          <div>
            <strong>ContentAI</strong>
          </div>
        </div>
        <h1>持续对话工作台</h1>
      </div>

      <aside class="control-card glass-surface">
        <div ref="accountPickerRoot" class="account-picker" @keydown.esc="closeAccountMenu">
          <button
            class="account-trigger"
            type="button"
            :aria-expanded="accountMenuOpen"
            aria-haspopup="listbox"
            @click="toggleAccountMenu"
            @keydown="handleAccountKeydown"
          >
            <span>{{ selectedAccountName }}</span>
            <ChevronDown :size="17" class="account-chevron" :class="{ open: accountMenuOpen }" />
          </button>
          <div v-if="accountMenuOpen" class="account-menu" role="listbox">
            <button
              v-for="(account, index) in store.accounts"
              :key="account.id"
              class="account-option"
              :class="{
                selected: account.id === store.accountId,
                highlighted: index === highlightedAccountIndex
              }"
              type="button"
              role="option"
              :aria-selected="account.id === store.accountId"
              @click="chooseAccount(account.id)"
            >
              <span>{{ account.name }}</span>
              <Check v-if="account.id === store.accountId" :size="15" />
            </button>
          </div>
        </div>
        <div class="metric-row">
          <div>
            <span>Sessions</span>
            <strong>{{ store.sessions.length }}</strong>
          </div>
          <div>
            <span>Messages</span>
            <strong>{{ store.messages.length }}</strong>
          </div>
        </div>
      </aside>
    </section>

    <section class="workspace-grid">
      <aside class="session-dock glass-surface">
        <div class="session-heading">
          <span>
            <History :size="16" />
            <strong>会话记录</strong>
          </span>
          <button class="session-new-action" type="button" aria-label="新建会话" @click="store.startNewSession">
            <MessageCirclePlus :size="16" />
          </button>
        </div>
        <div class="session-list">
          <button
            v-for="session in store.sessions"
            :key="session.session_id"
            class="session-row"
            :class="{ active: session.session_id === store.sessionId }"
            type="button"
            @click="store.loadSession(session.session_id)"
          >
            <strong>{{ sessionDisplayTitle(session.title, session.message_count) }}</strong>
            <small>{{ sessionMeta(session.message_count, session.latest_status) }}</small>
          </button>
          <p v-if="!store.sessions.length" class="session-empty">暂无会话记录</p>
        </div>
      </aside>

      <section class="chat-panel glass-surface">
        <header class="chat-header">
          <div class="chat-title-block">
            <h2>{{ selectedAccountName }}</h2>
          </div>
          <div class="chat-toolbar">
            <nav class="top-nav chat-nav" aria-label="会话窗口导航">
              <button class="nav-item active" type="button" aria-label="工作台">
                <MessageSquareText :size="16" />
                <span>工作台</span>
              </button>
              <button class="nav-item" type="button" aria-label="账号配置" @click="openAccountManager">
                <Settings :size="16" />
                <span>账号配置</span>
              </button>
            </nav>

            <button
              class="icon-pill"
              type="button"
              :aria-label="isDarkMode ? '切换到浅色模式' : '切换到深色模式'"
              :title="isDarkMode ? '浅色模式' : '深色模式'"
              @click="toggleTheme"
            >
              <Sun v-if="isDarkMode" :size="17" />
              <Moon v-else :size="17" />
            </button>
          </div>
        </header>

        <div ref="chatStream" class="chat-stream">
          <section v-if="!store.messages.length" class="empty-chat">
            <span class="empty-icon"><MessageSquareText :size="24" /></span>
            <h3>新会话已准备好</h3>
            <p>输入问题后，Agent 会结合会话上下文和长期记忆持续回应。</p>
          </section>
          <article
            v-for="(message, index) in store.messages"
            :key="index"
            class="message"
            :class="message.role"
          >
            <div class="avatar">{{ message.role === 'user' ? '你' : 'AI' }}</div>
            <div class="message-content" v-html="renderMessageContent(message)"></div>
          </article>
        </div>

        <div class="composer">
          <textarea
            ref="promptInput"
            v-model="prompt"
            rows="3"
            aria-label="对话输入"
            :placeholder="defaultPrompt"
            @keydown.enter.prevent.exact="submit"
            @keydown.ctrl.enter.prevent="submit"
            @keydown.meta.enter.prevent="submit"
          />
          <button class="primary-action send-action" :disabled="!store.canSubmit" @click="submit">
            <LoaderCircle v-if="store.status === 'running'" :size="17" class="spin" />
            <Play v-else :size="17" />
            <span>{{ store.status === 'running' ? '处理中' : '发送' }}</span>
          </button>
        </div>
      </section>
    </section>

    <p v-if="store.error" class="workspace-error error">{{ store.error }}</p>

    <div v-if="accountManagerOpen" class="modal-backdrop" @click.self="closeAccountManager">
      <section class="account-manager account-config-manager" role="dialog" aria-modal="true" aria-labelledby="account-manager-title">
        <header class="manager-header">
          <div>
          <h2 id="account-manager-title">账号配置</h2>
            <p>账号配置会作为 Agent 的长期系统上下文参与每轮对话。</p>
          </div>
          <span class="manager-count">{{ filteredAccounts.length }} / {{ store.accounts.length }} 个账号</span>
          <button class="icon-action" type="button" aria-label="关闭账号配置" @click="closeAccountManager">
            <X :size="18" />
          </button>
        </header>

        <div class="manager-body">
          <aside class="account-list">
            <div class="panel-title">
              <strong>账号列表</strong>
              <span>选择一个账号后编辑策略字段</span>
            </div>
            <div class="account-list-header">
              <label class="search-wrap">
                <Search :size="16" />
                <input
                  v-model="accountSearchTerm"
                  type="search"
                  autocomplete="off"
                   placeholder="输入账号名称进行搜索"
                   aria-label="搜索账号"
                />
              </label>
              <button class="new-account" type="button" @click="startNewAccount">
                <Plus :size="16" />
                <span>新增账号</span>
              </button>
            </div>
            <div class="account-list-scroll">
              <p v-if="!filteredAccounts.length" class="account-empty">暂无匹配账号，请尝试其他关键词。</p>
              <button
                v-for="account in filteredAccounts"
                :key="account.id"
                class="account-row"
                :class="{ selected: account.id === accountEditingId && accountFormMode === 'edit' }"
                type="button"
                @click="editAccount(account.id)"
              >
                <span class="account-row-main">
                  <strong>{{ account.name }}</strong>
                </span>
                <span class="account-row-desc">{{ account.positioning || "未填写账号定位" }}</span>
                <span v-if="account.id === store.accountId" class="account-row-badge">当前使用</span>
              </button>
            </div>
          </aside>

          <form class="account-form" @submit.prevent="saveAccount">
            <div v-if="accountFormLoading" class="form-loading">账号配置加载中...</div>
            <template v-else>
              <header class="form-head">
                <div>
                  <span class="form-badge">{{ accountFormMode === "edit" ? "编辑账号" : "新增账号" }}</span>
                  <h3>{{ accountFormMode === "edit" ? "编辑账号" : "新增账号" }}</h3>
                </div>
              </header>

              <section class="form-section strategy-section">
                <div class="section-title">
                  <h4>内容账号配置</h4>
                  <span>这些字段会注入每轮对话，驱动选题筛选和内容创作</span>
                </div>
                <fieldset class="hotspot-source-field">
                  <legend>热点来源</legend>
                  <div class="hotspot-source-grid">
                    <button
                      v-for="source in HOTSPOT_SOURCE_OPTIONS"
                      :key="source.id"
                      class="hotspot-source-option"
                      :class="{ selected: accountForm.hotspot_sources.includes(source.id) }"
                      type="button"
                      :aria-pressed="accountForm.hotspot_sources.includes(source.id)"
                      @click="toggleHotspotSource(source.id)"
                    >
                      <Check v-if="accountForm.hotspot_sources.includes(source.id)" :size="14" />
                      <span>{{ source.label }}</span>
                    </button>
                  </div>
                </fieldset>
                <div class="form-grid">
                  <label>
                    <span>账号名称</span>
                    <input ref="accountNameInput" v-model="accountForm.name" autocomplete="off" placeholder="例如：财经洞察账号" />
                  </label>
                </div>
                <label>
                  <span>账号定位</span>
                  <textarea
                    v-model="accountForm.positioning"
                    rows="6"
                    placeholder="描述账号人设、目标受众、内容边界、核心差异化和不适合覆盖的方向。"
                  />
                </label>
                <label>
                  <span>选题过滤评分提示词</span>
                  <textarea
                    v-model="accountForm.topic_scoring_prompt"
                    rows="8"
                    placeholder="定义候选选题的筛选、评分、排序标准，例如匹配度、传播潜力、风险、商业价值等。"
                  />
                </label>
                <label>
                  <span>内容创作提示词</span>
                  <textarea
                    v-model="accountForm.content_creation_prompt"
                    rows="8"
                    placeholder="定义标题、正文、脚本、口吻、结构、表达禁忌和输出格式偏好。"
                  />
                </label>
              </section>

              <p v-if="accountMessage" class="form-message" role="status" aria-live="polite">{{ accountMessage }}</p>
              <div class="form-actions">
                <button
                  class="danger-action"
                  type="button"
                  :disabled="accountFormMode !== 'edit' || accountSaving"
                  @click="deleteCurrentAccount"
                >
                  <Trash2 :size="16" />
                  <span>删除当前账号</span>
                </button>
                <button class="primary-action compact" type="submit" :disabled="accountSaving || !canSaveAccount">
                  <LoaderCircle v-if="accountSaving" :size="16" class="spin" />
                  <Save v-else :size="16" />
                  <span>{{ accountSaving ? "保存中..." : "保存账号" }}</span>
                </button>
              </div>
            </template>
          </form>
        </div>
      </section>
    </div>
  </main>
</template>







