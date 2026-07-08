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
  type ChatSessionSummary,
} from './services/api';
import { useWorkbenchStore } from './stores/workbench';

const store = useWorkbenchStore();
const defaultPrompt = '直接输入你想讨论或处理的问题';
const prompt = ref('');
const resumePrompt = ref('');
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
type DestructiveDeleteTarget = {
  kind: 'session' | 'account';
  id: string;
  label: string;
};
const destructiveDeleteTarget = ref<DestructiveDeleteTarget | null>(null);
const destructiveDeleteAcknowledged = ref(false);
const destructiveDeleteBusy = ref(false);
const markdownRenderer = new MarkdownIt({
  breaks: true,
  html: false,
  linkify: true
});

type HotspotSourceOption = {
  id: string;
  label: string;
  defaultEnabled?: boolean;
};

const HOTSPOT_SOURCE_GROUPS: { title: string; sources: HotspotSourceOption[] }[] = [
  {
    title: '已验证稳定',
    sources: [
      { id: '36kr', label: '36Kr', defaultEnabled: true },
      { id: 'cls', label: '财联社', defaultEnabled: true },
      { id: 'eeo', label: '经济观察报', defaultEnabled: true },
      { id: 'yicai', label: '第一财经', defaultEnabled: true },
      { id: 'huxiu', label: '虎嗅', defaultEnabled: true },
      { id: 'jiemian', label: '界面', defaultEnabled: true },
      { id: 'tmtpost', label: '钛媒体', defaultEnabled: true },
      { id: 'latepost', label: '晚点', defaultEnabled: true },
      { id: 'qbitai', label: '量子位', defaultEnabled: true },
      { id: 'leiphone', label: '雷峰网', defaultEnabled: true },
      { id: 'bloomberg', label: 'Bloomberg', defaultEnabled: true },
      { id: 'ft', label: 'FT', defaultEnabled: true },
      { id: 'wsj', label: 'WSJ', defaultEnabled: true },
      { id: 'techcrunch', label: 'TechCrunch', defaultEnabled: true }
    ]
  },
  {
    title: '聚合与补充',
    sources: [
      { id: 'caixin', label: '财新' },
      { id: 'vista', label: 'Vista 看天下' },
      { id: 'theverge', label: 'The Verge' },
      { id: 'ifanr', label: '爱范儿' }
    ]
  },
  {
    title: '待确认渠道',
    sources: [
      { id: 'stcn', label: '证券时报' }
    ]
  },
  {
    title: '社交热榜',
    sources: [
      { id: 'douyin', label: '抖音', defaultEnabled: true },
      { id: 'bilibili', label: 'Bilibili', defaultEnabled: true },
      { id: 'xiaohongshu', label: '小红书', defaultEnabled: true },
      { id: 'weibo', label: '微博', defaultEnabled: true },
      { id: 'aihot', label: 'AI HOT', defaultEnabled: true }
    ]
  }
];
const HOTSPOT_SOURCE_OPTIONS = HOTSPOT_SOURCE_GROUPS.flatMap((group) => group.sources);

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
  hotspot_sources: HOTSPOT_SOURCE_OPTIONS.filter((source) => source.defaultEnabled).map((source) => source.id)
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
const destructiveDeleteTitle = computed(() => {
  if (!destructiveDeleteTarget.value) return '';
  return destructiveDeleteTarget.value.kind === 'session' ? '删除会话' : '删除账号';
});
const destructiveDeleteDescription = computed(() => {
  if (!destructiveDeleteTarget.value) return '';
  return destructiveDeleteTarget.value.kind === 'session'
    ? '删除后会移除该会话的消息、运行记录和短期记忆。'
    : '删除后会移除该账号配置，且无法继续用于新的对话。';
});
const interruptSummary = computed(() => {
  const payload = store.interruptPayload;
  const interrupts = Array.isArray(payload.interrupts) ? payload.interrupts : [];
  const first = interrupts[0] as { value?: unknown } | undefined;
  if (first?.value) return typeof first.value === 'string' ? first.value : JSON.stringify(first.value);
  return 'Agent 需要你的确认或补充信息后继续。';
});
const isRunPending = computed(() => store.runLifecycle === 'running');

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

function resumeInterrupted() {
  const message = resumePrompt.value.trim();
  if (!message || !store.canResume) return;
  store.resume(message);
  resumePrompt.value = '';
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

function requestDeleteCurrentAccount() {
  if (accountFormMode.value !== 'edit' || !accountEditingId.value) return;
  openDestructiveDelete({
    kind: 'account',
    id: accountEditingId.value,
    label: accountForm.value.name || '当前账号'
  });
}

async function performAccountDelete(accountId: string) {
  accountSaving.value = true;
  accountMessage.value = '';
  try {
    await store.deleteAccount(accountId);
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

function sessionDisplayTitle(title: string, messageCount: number) {
  if (title && title !== 'New Session') return title;
  return messageCount > 0 ? '未命名会话' : '新会话';
}

function sessionMeta(messageCount: number) {
  return messageCount > 0 ? `${messageCount} 条消息` : '暂无消息';
}

function requestDeleteSession(session: ChatSessionSummary) {
  const title = sessionDisplayTitle(session.title, session.message_count);
  openDestructiveDelete({
    kind: 'session',
    id: session.session_id,
    label: title
  });
}

function openDestructiveDelete(target: DestructiveDeleteTarget) {
  destructiveDeleteTarget.value = target;
  destructiveDeleteAcknowledged.value = false;
}

function closeDestructiveDelete() {
  if (destructiveDeleteBusy.value) return;
  destructiveDeleteTarget.value = null;
  destructiveDeleteAcknowledged.value = false;
}

async function confirmDestructiveDelete() {
  if (!destructiveDeleteTarget.value || !destructiveDeleteAcknowledged.value) return;
  const target = destructiveDeleteTarget.value;
  destructiveDeleteBusy.value = true;
  try {
    if (target.kind === 'session') {
      await store.deleteSession(target.id);
    } else {
      await performAccountDelete(target.id);
    }
    destructiveDeleteTarget.value = null;
    destructiveDeleteAcknowledged.value = false;
  } catch (error) {
    if (target.kind === 'account') {
      accountMessage.value = error instanceof Error ? error.message : String(error);
    } else {
      store.error = error instanceof Error ? error.message : String(error);
    }
    destructiveDeleteTarget.value = null;
    destructiveDeleteAcknowledged.value = false;
  } finally {
    destructiveDeleteBusy.value = false;
  }
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

function assistantPlaceholderLabel(state?: string, content = '') {
  if (state === 'pending') return 'AI 正在思考';
  if (state === 'streaming') return content ? 'AI 正在生成' : 'AI 正在生成';
  return '';
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
        <div class="brand hero-brand" aria-label="ContentAI">
          <div class="brand-mark"><Sparkles :size="16" /></div>
          <div>
            <strong>ContentAI</strong>
          </div>
        </div>
      </div>

      <aside class="control-card glass-surface">
        <nav class="top-nav control-nav" aria-label="工作台导航">
          <button class="nav-item active" type="button" aria-label="工作台">
            <MessageSquareText :size="16" />
            <span>工作台</span>
          </button>
          <button class="nav-item" type="button" aria-label="账号配置" @click="openAccountManager">
            <Settings :size="16" />
            <span>账号配置</span>
          </button>
        </nav>
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
      </aside>
    </section>

    <section class="workspace-grid">
      <aside class="session-dock glass-surface">
        <div class="session-heading">
          <span>
            <History :size="16" />
            <strong>会话记录</strong>
            <em class="session-count-badge">{{ store.sessions.length }}</em>
          </span>
          <button class="session-new-action" type="button" aria-label="新建会话" @click="store.startNewSession">
            <MessageCirclePlus :size="16" />
          </button>
        </div>
        <div class="session-list">
          <div
            v-for="session in store.sessions"
            :key="session.session_id"
            class="session-row"
            :class="{ active: session.session_id === store.sessionId }"
            role="button"
            tabindex="0"
            @click="store.loadSession(session.session_id)"
            @keydown.enter.prevent="store.loadSession(session.session_id)"
            @keydown.space.prevent="store.loadSession(session.session_id)"
          >
            <span class="session-row-main">
              <strong>{{ sessionDisplayTitle(session.title, session.message_count) }}</strong>
              <small>{{ sessionMeta(session.message_count) }}</small>
            </span>
            <button
              class="session-delete-action"
              type="button"
              aria-label="删除会话"
              title="删除会话"
              @click.stop="requestDeleteSession(session)"
            >
              <Trash2 :size="15" />
            </button>
          </div>
          <p v-if="!store.sessions.length" class="session-empty">暂无会话记录</p>
        </div>
      </aside>

      <section class="chat-panel glass-surface">
        <div ref="chatStream" class="chat-stream">
          <p v-if="store.statusNotice" class="status-notice">
            <LoaderCircle :size="15" class="spin" />
            <span>{{ store.statusNotice }}</span>
          </p>
          <section v-if="!store.messages.length" class="empty-chat">
            <span class="empty-icon"><MessageSquareText :size="24" /></span>
            <h3>新会话已准备好</h3>
            <p>输入问题后，Agent 会结合会话上下文和长期记忆持续回应。</p>
          </section>
          <article
            v-for="(message, index) in store.messages"
            :key="index"
            class="message"
            :class="[
              message.role,
              message.assistant_state === 'pending' ? 'assistant-pending' : '',
              message.assistant_state === 'streaming' ? 'assistant-streaming' : ''
            ]"
          >
            <div class="avatar">{{ message.role === 'user' ? '你' : 'AI' }}</div>
            <div class="message-content">
              <template v-if="message.role === 'assistant' && message.assistant_state && message.assistant_state !== 'normal'">
                <div class="assistant-progress">
                  <LoaderCircle :size="14" class="spin assistant-progress-spinner" />
                  <span>{{ assistantPlaceholderLabel(message.assistant_state, message.content) }}</span>
                </div>
                <div v-if="message.content" v-html="renderMessageContent(message)"></div>
              </template>
              <div v-else v-html="renderMessageContent(message)"></div>
            </div>
          </article>
        </div>

        <div v-if="store.canResume" class="resume-panel">
          <div class="resume-copy">
            <strong>等待确认</strong>
            <span>{{ interruptSummary }}</span>
          </div>
          <div class="resume-actions">
            <input
              v-model="resumePrompt"
              type="text"
              autocomplete="off"
              placeholder="输入确认或补充信息"
              aria-label="恢复对话输入"
              @keydown.enter.prevent="resumeInterrupted"
            />
            <button class="primary-action" type="button" :disabled="!resumePrompt.trim()" @click="resumeInterrupted">
              <Play :size="17" />
              <span>继续</span>
            </button>
          </div>
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
            <LoaderCircle v-if="isRunPending" :size="17" class="spin" />
            <Play v-else :size="17" />
            <span>{{ isRunPending ? '处理中' : '发送' }}</span>
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
                  <div class="hotspot-source-groups">
                    <section v-for="group in HOTSPOT_SOURCE_GROUPS" :key="group.title" class="hotspot-source-group">
                      <h5>{{ group.title }}</h5>
                      <div class="hotspot-source-grid">
                        <button
                          v-for="source in group.sources"
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
                    </section>
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
                  @click="requestDeleteCurrentAccount"
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

    <div
      v-if="destructiveDeleteTarget"
      class="modal-backdrop confirm-backdrop"
      @click.self="closeDestructiveDelete"
    >
      <section
        class="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="destructive-confirm-title"
      >
        <header class="confirm-header">
          <span class="confirm-icon"><Trash2 :size="18" /></span>
          <button
            class="icon-action"
            type="button"
            aria-label="关闭删除确认"
            :disabled="destructiveDeleteBusy"
            @click="closeDestructiveDelete"
          >
            <X :size="18" />
          </button>
        </header>
        <div class="confirm-copy">
          <h2 id="destructive-confirm-title">{{ destructiveDeleteTitle }}</h2>
          <p>
            <strong>{{ destructiveDeleteTarget.label }}</strong>
          </p>
          <p>{{ destructiveDeleteDescription }}此操作不可恢复。</p>
        </div>
        <label class="confirm-ack">
          <input v-model="destructiveDeleteAcknowledged" type="checkbox" />
          <span>我理解此操作不可恢复</span>
        </label>
        <footer class="confirm-actions">
          <button
            class="secondary-action"
            type="button"
            :disabled="destructiveDeleteBusy"
            @click="closeDestructiveDelete"
          >
            取消
          </button>
          <button
            class="danger-action confirm-delete-action"
            type="button"
            :disabled="!destructiveDeleteAcknowledged || destructiveDeleteBusy"
            @click="confirmDestructiveDelete"
          >
            <LoaderCircle v-if="destructiveDeleteBusy" :size="16" class="spin" />
            <Trash2 v-else :size="16" />
            <span>{{ destructiveDeleteBusy ? "删除中..." : "确认删除" }}</span>
          </button>
        </footer>
      </section>
    </div>
  </main>
</template>







