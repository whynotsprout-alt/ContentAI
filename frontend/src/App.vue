<script setup lang="ts">
import {
  Check,
  ChevronDown,
  CircleCheck,
  CircleDashed,
  FileText,
  Layers3,
  LoaderCircle,
  MessageSquareText,
  Play,
  Plus,
  Save,
  RotateCcw,
  Settings,
  Search,
  Sparkles,
  Trash2,
  X
} from '@lucide/vue';
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  api,
  type AccountDetail,
  type AccountPayload,
  type Artifact,
  type HotspotPlatformOption,
  type SystemConfig,
  type SystemConfigPayload,
} from './services/api';
import { useWorkbenchStore } from './stores/workbench';

const store = useWorkbenchStore();
const defaultPrompt = '请告诉我你的内容创作方向（例如：请给我几个选题建议）';
const prompt = ref('');
const accountMenuOpen = ref(false);
const accountPickerRoot = ref<HTMLElement | null>(null);
const chatStream = ref<HTMLElement | null>(null);
const promptInput = ref<HTMLTextAreaElement | null>(null);
const highlightedAccountIndex = ref(0);
const accountManagerOpen = ref(false);
const systemConfigManagerOpen = ref(false);
const accountFormLoading = ref(false);
const accountSaving = ref(false);
const accountFormMode = ref<'create' | 'edit'>('edit');
const accountMessage = ref('');
const accountSearchTerm = ref('');
const accountForm = ref({
  id: '',
  name: '',
  description: '',
  audience: '',
  preferred_directions: '',
  boundaries: '',
  viral_patterns: '',
  style_prompt: ''
});
const systemConfigForm = ref({
  hotspot_platforms: [] as string[],
  topic_filter_prompt: '',
  topic_scoring_prompt: ''
});
const platformOptions = ref<HotspotPlatformOption[]>([]);
const accountNameInput = ref<HTMLInputElement | null>(null);
const systemConfigLoading = ref(false);
const systemConfigSaving = ref(false);
const systemConfigMessage = ref('');
const DEFAULT_PLATFORM_OPTIONS: HotspotPlatformOption[] = [
  { id: '36kr', label: '36氪 RSS' },
  { id: 'huxiu', label: '虎嗅 RSS' },
  { id: 'ifanr', label: '爱范儿 RSS' },
  { id: 'douyin', label: '抖音' },
  { id: 'bilibili', label: 'Bilibili' },
  { id: 'xiaohongshu', label: '小红书' },
  { id: 'weibo', label: '微博' },
  { id: 'aihot', label: 'AIHOT' },
];
const DEFAULT_SYSTEM_CONFIG: SystemConfig = {
  hotspot_platforms: DEFAULT_PLATFORM_OPTIONS.map((platform) => platform.id),
  topic_filter_prompt: `你是内容选题筛选助手。请按以下规则处理候选热点：
1. 过滤政治、违法、低价值、重复或与账号方向严重不符的内容。
2. 优先保留热度高、可写性强、时效性好的主题。
3. 输出必须是 JSON 数组，字段包含 title、reason、score、tags。`,
  topic_scoring_prompt: `请对每个候选主题给出 1-100 分数。
4 个维度：相关性、用户价值、可写性、时效性。
输出 JSON 数组，每条仅包含 title、score、reason。`
};

const DEFAULT_ACCOUNT_FORM: {
  id: string;
  name: string;
  description: string;
  audience: string;
  preferred_directions: string;
  boundaries: string;
  viral_patterns: string;
  style_prompt: string;
} = {
  id: '',
  name: '',
  description: '',
  audience: '',
  preferred_directions: '',
  boundaries: '',
  viral_patterns: '',
  style_prompt: ''
};
const allPlatformIds = () => {
  const options = platformOptions.value.length ? platformOptions.value : DEFAULT_PLATFORM_OPTIONS;
  const ids: string[] = [];
  for (const option of options) {
    if (!ids.includes(option.id)) {
      ids.push(option.id);
    }
  }
  return ids;
};

const steps = computed(() => {
  const fromRun = store.runInfo?.steps ?? [];
  if (fromRun.length) return fromRun;
  return [
    { name: 'collect_hotspots', label: '获取热点', status: 'pending', error: '' },
    { name: 'filter_hotspots', label: '筛选热点', status: 'pending', error: '' },
    { name: 'score_topics', label: '主题评分', status: 'pending', error: '' },
    { name: 'deep_search_topic', label: '深度搜索', status: 'pending', error: '' },
    { name: 'generate_content_draft', label: '生成内容草稿', status: 'pending', error: '' },
    { name: 'read_artifact', label: '读取产物', status: 'pending', error: '' }
  ];
});

const canSelectTopic = computed(() => store.status === 'waiting_for_topic_confirmation');
const canConfirmHotspots = computed(
  () => store.status === 'waiting_for_topic_confirmation' && Boolean(store.$state.pendingHotspotSummary)
);
const canConfirmResearch = computed(() => store.status === 'waiting_for_research_confirmation');

const candidateEvents = computed(() => store.pendingTopics);
const pendingHotspotSummary = computed(() => store.$state.pendingHotspotSummary);
const filteredAccounts = computed(() => {
  const term = accountSearchTerm.value.trim().toLowerCase();
  if (!term) return store.accounts;
  return store.accounts.filter((account) => {
    const haystack = `${account.id} ${account.name} ${account.description}`.toLowerCase();
    return haystack.includes(term);
  });
});
const accountIdConflict = computed(() => {
    const payload = accountPayload();
  if (!payload.id || accountFormMode.value === 'edit') return false;
  return store.accounts.some((account) => account.id === payload.id);
});
const canSaveAccount = computed(() => {
  const payload = accountPayload();
  return Boolean(payload.id && payload.name) && !accountIdConflict.value;
});
const canSaveSystemConfig = computed(() => true);

const statusLabel = computed(() => {
  const labels: Record<string, string> = {
    idle: '空闲',
    running: '运行中',
    waiting_for_topic_confirmation: canConfirmHotspots.value ? '等待热点确认' : '等待选题确认',
    waiting_for_research_confirmation: '等待素材确认',
    completed: '已完成',
    failed: '执行失败'
  };
  return labels[store.status] ?? store.status;
});

const isMarkdownArtifact = (artifact: Artifact): boolean => {
  const mediaType = artifact.media_type.toLowerCase();
  const title = artifact.title.toLowerCase();
  const url = artifact.url.toLowerCase();
  const kind = artifact.kind.toLowerCase();

  return (
    mediaType.includes('markdown') ||
    mediaType.includes('text/markdown') ||
    title.endsWith('.md') ||
    url.endsWith('.md') ||
    kind.includes('markdown') ||
    /markdown/i.test(title) ||
    /markdown/i.test(url) ||
    /\\.md($|\\?|#)/i.test(url)
  );
};

const markdownArtifacts = computed(() => store.artifacts.filter(isMarkdownArtifact));

interface ArtifactPreviewContext {
  title: string;
  markdownUrl: string;
  markdownText: string;
}

const artifactPreviewOpen = ref(false);
const artifactPreview = ref<ArtifactPreviewContext>({
  title: '',
  markdownUrl: '',
  markdownText: '',
});


const activeStepCount = computed(() => {
  return steps.value.filter((step) => step.status === 'completed').length;
});

const runningStep = computed(() => steps.value.find((step) => step.status === 'running'));

const progressPercent = computed(() => {
  const completed = activeStepCount.value;
  const runningWeight = runningStep.value ? 0.45 : 0;
  return Math.min(100, Math.round(((completed + runningWeight) / steps.value.length) * 100));
});

const selectedAccountName = computed(() => {
  return store.selectedAccount?.name ?? '请选择账号';
});

const currentStageLabel = computed(() => {
  if (runningStep.value) return runningStep.value.label;
  if (store.status === 'waiting_for_topic_confirmation') {
    return canConfirmHotspots.value ? '等待热点确认' : '等待选题确认';
  }
  if (store.status === 'waiting_for_research_confirmation') return '等待素材确认';
  if (store.status === 'running') return '任务进行中';
  if (store.status === 'completed') return '任务已完成';
  if (store.status === 'failed') return '执行失败';
  return '等待用户输入选题策略';
});

function normalizeSentenceForCompare(value: string) {
  return value
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\[(.*?)\]\([^)]+\)/g, '$1')
    .replace(/["“”‘’*_\-+=~\u0060{}\[\]<>|\/,:;.!?，。；：！？]/g, '')
    .replace(/\s+/g, '')
    .trim();
}

function dedupeSummarySentences(value: string) {
  const chunks = value.match(/[^。！？!?;；\n]+[。！？!?;；]?\s*/g) ?? [];
  const seen = new Set<string>();
  const deduped: string[] = [];
  for (const chunk of chunks) {
    const trimmed = chunk.trim();
    if (!trimmed) {
      continue;
    }
    const normalized = normalizeSentenceForCompare(trimmed);
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    deduped.push(trimmed);
  }
  return deduped.join('');
}

function sanitizeResearchPackSummary(value: string, topicTitle?: string) {
  let deduped = dedupeSummarySentences(value).trim();
  deduped = deduped
    .replace(/^深度(?:搜索|检索)资料包[:：]\s*/i, '')
    .replace(/^资料包标题[:：]\s*/i, '')
    .trim();
  if (topicTitle && deduped.toLowerCase().startsWith(topicTitle.toLowerCase())) {
    deduped = deduped.slice(topicTitle.length).replace(/^[:：\-—–\s]+/, '').trim();
  }
  deduped = deduped
    .replace(/^生成时间[:：][^。！？!?;；\n]+[。！？!?;；]?\s*/i, '')
    .replace(/^搜索来源[:：][^。！？!?;；\n]+[。！？!?;；]?\s*/i, '')
    .replace(/^检索源[:：][^。！？!?;；\n]+[。！？!?;；]?\s*/i, '')
    .replace(/^上游评分[:：][^。！？!?;；\n]+[。！？!?;；]?\s*/i, '')
    .replace(/^推荐度[:：][^。！？!?;；\n]+[。！？!?;；]?\s*/i, '')
    .trim();
  if (!deduped) {
    return '';
  }
  return deduped.length > 1000 ? `${deduped.slice(0, 1000)}...` : deduped;
}

function extractResearchPackSummaryFromMarkdown(value: string, topicTitle?: string) {
  const normalized = (value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const preferredSections = ['内容概要', '关键事实', '重要上下文', '数据要点'];
  const lines = normalized.split('\n');
  const collected: string[] = [];
  let active = false;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    const heading = line.match(/^##\s+(.+)$/);
    if (heading) {
      active = preferredSections.some((section) => heading[1].trim().startsWith(section));
      if (collected.length && !active) {
        break;
      }
      continue;
    }
    if (!active || !line || line.startsWith('|')) {
      continue;
    }
    const content = line
      .replace(/^[-*]\s+/, '')
      .replace(/\s+\[[^\]]+\]\s*$/, '')
      .trim();
    if (content && !content.includes('暂未提取到有效事实')) {
      collected.push(content);
    }
    if (collected.length >= 6) {
      break;
    }
  }

  const sectionSummary = sanitizeResearchPackSummary(markdownToPlainText(collected.join('\n')), topicTitle);
  if (sectionSummary) {
    return sectionSummary;
  }

  const filtered = markdownToPlainText(normalized)
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => {
      if (!line) return false;
      if (/^(深度(?:搜索|检索)资料包|资料包标题|生成时间|搜索来源|检索源|上游评分|推荐度|结论标签|推荐角度|待补充)[:：]/.test(line)) return false;
      if (/^(检索说明|参考来源总览|来源快照)$/.test(line)) return false;
      if (topicTitle && normalizeSentenceForCompare(line).startsWith(normalizeSentenceForCompare(topicTitle))) return false;
      return true;
    })
    .slice(0, 6)
    .join('\n');

  return sanitizeResearchPackSummary(filtered, topicTitle);
}

const researchPackForDisplay = computed(() => store.pendingResearch);
const researchPackSummary = computed(() => {
  const pack = store.pendingResearch;
  if (!pack) {
    return '';
  }
  const directSummary = sanitizeResearchPackSummary(
    (pack.summary || '')
      .replace(/\[(.*?)\]\([^)]+\)/g, '$1')
      .replace(/https?:\/\/\S+/g, '')
      .replace(/\s{2,}/g, ' ')
      .trim(),
    pack.topicTitle
  );
  if (directSummary) {
    return directSummary;
  }
  const trimmed = extractResearchPackSummaryFromMarkdown(pack.markdownText, pack.topicTitle);
  if (!trimmed) {
    return '暂无可读内容，请先刷新深度搜索资料包。';
  }
  return trimmed;
});

const artifactPreviewRenderedContent = computed(() => {
  if (!artifactPreview.value.markdownText) {
    return '<p>暂无可读内容。</p>';
  }
  return markdownToHtml(artifactPreview.value.markdownText);
});

onMounted(() => {
  void api.hotspotPlatformOptions()
    .then((options) => {
      platformOptions.value = options.length ? options : DEFAULT_PLATFORM_OPTIONS;
    })
    .catch(() => {
      platformOptions.value = DEFAULT_PLATFORM_OPTIONS;
    });
  void loadSystemConfig();
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
  store.submit(message);
  prompt.value = '';
  nextTick(() => {
    promptInput.value?.focus();
  });
}

function selectTopic(index: number) {
  store.selectTopic(index);
}

function confirmResearch() {
  store.confirmResearch();
}

function confirmHotspots() {
  store.confirmHotspots();
}

function listToText(value: string[] | undefined): string {
  return (value ?? []).join('\n');
}

function textToList(value: string): string[] {
  return value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);
}

function resetAccountForm() {
  accountForm.value = { ...DEFAULT_ACCOUNT_FORM };
}

function hydrateAccountForm(account: AccountDetail) {
  accountForm.value = {
    id: account.id,
    name: account.name,
    description: account.description,
    audience: account.audience,
    preferred_directions: listToText(account.preferred_directions),
    boundaries: listToText(account.boundaries),
    viral_patterns: listToText(account.viral_patterns),
    style_prompt: account.style_prompt
  };
}

function accountPayload(): AccountPayload {
  return {
    id: accountForm.value.id.trim(),
    name: accountForm.value.name.trim(),
    description: accountForm.value.description.trim(),
    audience: accountForm.value.audience.trim(),
    preferred_directions: textToList(accountForm.value.preferred_directions),
    boundaries: textToList(accountForm.value.boundaries),
    viral_patterns: textToList(accountForm.value.viral_patterns),
    style_prompt: accountForm.value.style_prompt.trim()
  };
}

function isSystemPlatformSelected(platformId: string): boolean {
  return systemConfigForm.value.hotspot_platforms.includes(platformId);
}

function applyFilterPromptPreset(value: string) {
  systemConfigForm.value.topic_filter_prompt = value;
}

function toggleSystemPlatform(platformId: string) {
  const current = new Set(systemConfigForm.value.hotspot_platforms);
  if (current.has(platformId)) {
    current.delete(platformId);
  } else {
    current.add(platformId);
  }
  systemConfigForm.value.hotspot_platforms = [...current];
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

function openSystemConfigManager() {
  systemConfigManagerOpen.value = true;
  void loadSystemConfig();
}

function closeSystemConfigManager() {
  systemConfigManagerOpen.value = false;
  systemConfigMessage.value = '';
}

async function saveAccount() {
  const payload = accountPayload();
  if (!canSaveAccount.value) {
    accountMessage.value = '账号 ID 和账号名称不能为空。';
    return;
  }
  accountSaving.value = true;
  accountMessage.value = '';
  try {
    const account =
      accountFormMode.value === 'create'
        ? await store.createAccount(payload)
        : await store.updateAccount(payload.id, {
            name: payload.name,
            description: payload.description,
            audience: payload.audience,
            preferred_directions: payload.preferred_directions,
            boundaries: payload.boundaries,
            viral_patterns: payload.viral_patterns,
            style_prompt: payload.style_prompt
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

function hydrateSystemConfig(config: SystemConfig) {
  systemConfigForm.value = {
    hotspot_platforms: config.hotspot_platforms || [],
    topic_filter_prompt: config.topic_filter_prompt || '',
    topic_scoring_prompt: config.topic_scoring_prompt || ''
  };
}

function buildSystemConfigPayload(): SystemConfigPayload {
  return {
    hotspot_platforms: systemConfigForm.value.hotspot_platforms,
    topic_filter_prompt: systemConfigForm.value.topic_filter_prompt.trim(),
    topic_scoring_prompt: systemConfigForm.value.topic_scoring_prompt.trim(),
  };
}

function resetSystemConfigToDefault() {
  systemConfigForm.value = {
    hotspot_platforms: allPlatformIds(),
    topic_filter_prompt: DEFAULT_SYSTEM_CONFIG.topic_filter_prompt,
    topic_scoring_prompt: DEFAULT_SYSTEM_CONFIG.topic_scoring_prompt
  };
}

async function loadSystemConfig() {
  systemConfigLoading.value = true;
  systemConfigMessage.value = '';
  try {
    const config = await api.getSystemConfig();
    hydrateSystemConfig(config);
  } catch (error) {
    hydrateSystemConfig(DEFAULT_SYSTEM_CONFIG);
    systemConfigMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    systemConfigLoading.value = false;
  }
}

async function saveSystemConfig() {
  systemConfigSaving.value = true;
  systemConfigMessage.value = '';
  try {
    const payload = buildSystemConfigPayload();
    const updated = await api.updateSystemConfig(payload);
    hydrateSystemConfig(updated);
    systemConfigMessage.value = '系统配置保存成功。';
    closeSystemConfigManager();
  } catch (error) {
    systemConfigMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    systemConfigSaving.value = false;
  }
}

async function deleteCurrentAccount() {
  if (accountFormMode.value !== 'edit' || !accountForm.value.id) return;
  const accountLabel = accountForm.value.name || accountForm.value.id;
  const confirmedOnce = window.confirm(`确认删除账号【${accountLabel}】吗？此操作将执行账号移除。`);
  if (!confirmedOnce) return;
  const confirmedTwice = window.confirm(`请再次确认：确认彻底删除账号【${accountLabel}】吗？删除后不可恢复。`);
  if (!confirmedTwice) return;
  accountSaving.value = true;
  accountMessage.value = '';
  try {
    const deletedId = accountForm.value.id;
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

function stepStatusLabel(status: string) {
  const labels: Record<string, string> = {
    pending: '待执行',
    running: '执行中',
    completed: '已完成',
    failed: '执行失败'
  };
  return labels[status] ?? status;
}

function formatArtifactName(artifact: Artifact): string {
  const title = artifact.title.trim();
  const titleLower = title.toLowerCase();
  if (titleLower.endsWith('.md')) {
    return title.replace(/^\d+_/, '');
  }
  return title || 'Markdown文件';
}

function markdownToPlainText(value: string): string {
  return value
    .replace(/\r\n/g, '\n')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/!\[[^\]]*\]\([^)]+\)/g, '')
    .replace(/\[(.*?)\]\(([^)]+)\)/g, '$1')
    .replace(/[`*_~>#-]/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function markdownToHtml(value: string): string {
  const raw = (value ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const codeBlocks: string[] = [];
  const codePlaceholder = '__CBLOCK__';
  const withPlaceholders = raw.replace(/```[^\n]*\n[\s\S]*?```/g, (block) => {
    const body = block
      .replace(/^```[^\n]*\n?/, '')
      .replace(/```$/, '')
      .replace(/\n$/, '');
    const index = codeBlocks.length;
    codeBlocks.push(`<pre><code>${escapeHtml(body)}</code></pre>`);
    return `${codePlaceholder}${index}${codePlaceholder}`;
  });

  const safe = escapeHtml(withPlaceholders);
  const lines = safe.split('\n');
  const out: string[] = [];
  let paragraph: string[] = [];
  let listType: 'ul' | 'ol' | null = null;

  const closeParagraph = () => {
    if (!paragraph.length) return;
    const content = paragraph.join('\n');
    out.push(`<p>${renderInlineMarkdown(content.replace(/\n/g, '<br/>'))}</p>`);
    paragraph = [];
  };

  const closeList = () => {
    if (!listType) return;
    out.push(`</${listType}>`);
    listType = null;
  };

  const openList = (nextType: 'ul' | 'ol') => {
    if (listType === nextType) return;
    closeList();
    out.push(`<${nextType}>`);
    listType = nextType;
  };

  const closeAll = () => {
    closeParagraph();
    closeList();
  };

  for (const rawLine of lines) {
    const line = rawLine;
    if (!line.trim()) {
      closeAll();
      continue;
    }

    const headingMatch = line.match(/^(\s*)(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      closeAll();
      const level = headingMatch[2].length;
      const text = headingMatch[3].trim();
      out.push(`<h${level}>${renderInlineMarkdown(text)}</h${level}>`);
      continue;
    }

    if (/^[-*_]{3,}\s*$/.test(line.trim())) {
      closeAll();
      out.push('<hr/>');
      continue;
    }

    const unorderedMatch = line.match(/^(\s*)[-*+]\s+(.*)$/);
    if (unorderedMatch) {
      closeParagraph();
      openList('ul');
      out.push(`<li>${renderInlineMarkdown(unorderedMatch[2].trim())}</li>`);
      continue;
    }

    const orderedMatch = line.match(/^(\s*)\d+\.\s+(.*)$/);
    if (orderedMatch) {
      closeParagraph();
      openList('ol');
      out.push(`<li>${renderInlineMarkdown(orderedMatch[2].trim())}</li>`);
      continue;
    }

    const blockquoteMatch = line.match(/^\s*>\s?(.*)$/);
    if (blockquoteMatch) {
      closeAll();
      out.push(`<blockquote>${renderInlineMarkdown(blockquoteMatch[1] || '')}</blockquote>`);
      continue;
    }

    closeList();
    paragraph.push(line.trim());
  }

  closeAll();

  const withText = out.join('\n');
  const placeholderRegExp = new RegExp(`${codePlaceholder}(\\d+)${codePlaceholder}`, 'g');
  let html = withText.replace(placeholderRegExp, (_, index) => {
    const item = codeBlocks[Number(index)];
    return item || '';
  });
  if (!html.trim()) {
    return `<p>暂无可读内容。</p>`;
  }
  return html;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function sanitizeHref(href: string): string {
  const safe = href.trim();
  if (/^(https?:\/\/|mailto:|tel:)/i.test(safe)) {
    return safe;
  }
  return '#';
}

function renderInlineMarkdown(value: string): string {
  let result = value
    .replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (match, label, href) => {
      return `<a href="${sanitizeHref(String(href))}" target="_blank" rel="noreferrer noopener">${label}</a>`;
    })
    .replace(/!\[([^\]\n]*)\]\([^)\n]+\)/g, (_match, alt) => `![${alt}]`);

  result = result
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/_([^_\n]+)_/g, '<em>$1</em>')
    .replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

  return result;
}

async function openArtifactPreview(artifact: Artifact) {
  if (!isMarkdownArtifact(artifact)) {
    return;
  }

  const markdownUrl = api.artifactUrl(artifact);
  artifactPreview.value = {
    title: formatArtifactName(artifact),
    markdownUrl,
    markdownText: '正在加载产出预览...',
  };
  artifactPreviewOpen.value = true;

  try {
    const response = await fetch(markdownUrl);
    if (!response.ok) {
      throw new Error('请求失败');
    }
    artifactPreview.value.markdownText = await response.text();
  } catch {
    artifactPreview.value.markdownText = '产出预览读取失败，请稍后重试。';
  }
}

function closeArtifactPreview() {
  artifactPreviewOpen.value = false;
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
  <main class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark"><Sparkles :size="18" /></div>
        <div>
          <strong>ContentAI</strong>
          <span>内容创作工作台</span>
        </div>
      </div>

      <nav class="side-nav" aria-label="侧边栏导航">
        <button class="nav-item active">
          <MessageSquareText :size="16" />
          <span>工作台</span>
        </button>
        <button class="nav-item" type="button" @click="openAccountManager">
          <Settings :size="16" />
          <span>账号配置</span>
        </button>
        <button class="nav-item" type="button" @click="openSystemConfigManager">
          <Layers3 :size="16" />
          <span>全局配置</span>
        </button>
      </nav>

      <section class="sidebar-block">
        <div class="sidebar-heading">
          <Layers3 :size="16" />
          <strong>运行状态</strong>
        </div>
        <div class="sidebar-status-line">
          <span>当前状态</span>
          <strong>{{ statusLabel }}</strong>
        </div>
        <p class="current-stage sidebar-stage">{{ currentStageLabel }}</p>
        <div
          class="progress-track sidebar-progress-track"
          role="progressbar"
          aria-label="运行进度"
          :aria-valuenow="progressPercent"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <span :style="{ transform: `scaleX(${progressPercent / 100})` }"></span>
        </div>
        <p class="sidebar-progress-meta">
          <strong>{{ activeStepCount }}</strong>
          / {{ steps.length }} 个完成
        </p>
      </section>

      <section class="sidebar-block">
        <div class="sidebar-heading">
          <Sparkles :size="16" />
          <strong>运行步骤</strong>
        </div>
        <ol class="sidebar-steps">
          <li v-for="step in steps" :key="step.name" :class="step.status">
            <CircleCheck v-if="step.status === 'completed'" :size="14" />
            <LoaderCircle v-else-if="step.status === 'running'" :size="14" class="spin" />
            <CircleDashed v-else :size="14" />
            <div>
              <strong>{{ step.label }}</strong>
              <small>{{ stepStatusLabel(step.status) }}</small>
            </div>
          </li>
        </ol>
      </section>

    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <h1>内容创作工作台</h1>
          <p>先选择账号并完善策略后开始创作。热点、筛题、评分策略由全局统一控制。</p>
        </div>
        <div class="selectors">
          <div ref="accountPickerRoot" class="account-picker" @keydown.esc="closeAccountMenu">
            <span class="field-label">账号</span>
            <button
              class="account-trigger"
              type="button"
              :aria-expanded="accountMenuOpen"
              aria-haspopup="listbox"
              @click="toggleAccountMenu"
              @keydown="handleAccountKeydown"
            >
              <span>{{ selectedAccountName }}</span>
              <ChevronDown :size="16" class="account-chevron" :class="{ open: accountMenuOpen }" />
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
        </div>
      </header>

      <div class="content-grid">
        <section class="chat-panel">
          <div ref="chatStream" class="chat-stream">
            <article
              v-for="(message, index) in store.messages"
              :key="index"
              class="message"
              :class="message.role"
            >
              <div class="avatar">{{ message.role === 'user' ? '用户' : 'AI' }}</div>
              <p>{{ message.content }}</p>
            </article>
          </div>

          <div class="composer">
            <textarea
              ref="promptInput"
              v-model="prompt"
              rows="3"
               aria-label="内容输入提示"
              :placeholder="defaultPrompt"
              @keydown.enter.prevent.exact="submit"
              @keydown.ctrl.enter.prevent="submit"
              @keydown.meta.enter.prevent="submit"
            />
            <button class="primary-action" :disabled="!store.canSubmit" @click="submit">
              <LoaderCircle v-if="store.status === 'running'" :size="17" class="spin" />
              <Play v-else :size="17" />
              <span>{{ store.status === 'running' ? '处理中' : '发送' }}</span>
            </button>
          </div>
        </section>

        <aside class="right-rail">
          <section class="rail-block">
            <div class="rail-heading">
              <Sparkles :size="17" />
              <strong>候选热点</strong>
            </div>
            <div v-if="candidateEvents.length" class="candidate-list">
              <div class="candidate-scroll">
                <article
                  v-for="(topic, index) in candidateEvents"
                  :key="`${topic.title}-${index}`"
                  class="candidate"
                >
                  <span class="candidate-label">{{ topic.decision_label }}</span>
                  <strong>{{ topic.title }}</strong>
                  <small>热度：{{ topic.hit_potential }}</small>
                  <button v-if="canSelectTopic" class="inline-action" @click="selectTopic(index)">选择该题</button>
                </article>
              </div>
            </div>
            <div v-else-if="canConfirmHotspots" class="candidate-scroll">
              <article class="candidate">
                <span class="candidate-label">热点采集结果</span>
                <strong>请先确认热点采集结果</strong>
                <p>{{ pendingHotspotSummary }}</p>
                <button class="inline-action" @click="confirmHotspots">确认并继续匹配候选</button>
              </article>
            </div>
            <div v-else-if="store.status === 'running'" class="skeleton-list" aria-label="选题加载中">
              <span></span>
              <span></span>
              <span></span>
            </div>
            <p v-else class="empty">候选热点尚未生成，请先获取热点后再做确认。</p>
          </section>

          <section class="rail-block">
            <div class="rail-heading">
              <FileText :size="17" />
              <strong>深度搜索资料包</strong>
            </div>
            <div v-if="store.pendingResearch">
              <p>资料包标题：{{ researchPackForDisplay?.topicTitle }}</p>
              <p class="research-summary">
                {{ researchPackSummary }}
              </p>
              <button
                class="inline-action"
                :disabled="!canConfirmResearch"
                @click="confirmResearch"
              >
                资料包确认完成后继续生成内容
              </button>
            </div>
            <p v-else class="empty">深度搜索资料包尚未准备，请先完成选题确认后再进行查看。</p>
          </section>

          <section class="rail-block">
            <div class="rail-heading">
              <FileText :size="17" />
               <strong>生成产出</strong>
            </div>
            <div v-if="markdownArtifacts.length" class="artifact-list">
              <button
                v-for="artifact in markdownArtifacts"
                :key="artifact.id"
                type="button"
                class="artifact-link"
                @click="openArtifactPreview(artifact)"
              >
                <span>{{ formatArtifactName(artifact) }}</span>
              </button>
            </div>
            <div v-else-if="store.status === 'running'" class="skeleton-list compact" aria-label="稿件生成中">
              <span></span>
              <span></span>
            </div>
            <p v-else class="empty">暂未生成产出内容，请先完成素材确认。生成结果将显示在这里。</p>
          </section>

          <p v-if="store.error" class="error">{{ store.error }}</p>
        </aside>
      </div>
    </section>

    <div v-if="accountManagerOpen" class="modal-backdrop" @click.self="closeAccountManager">
      <section class="account-manager account-config-manager" role="dialog" aria-modal="true" aria-labelledby="account-manager-title">
        <header class="manager-header">
          <div>
          <h2 id="account-manager-title">账号配置</h2>
            <p>账号详情页仅配置账号策略字段，热点来源 / 筛题提示词 / 评分提示词由系统配置统一管理。</p>
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
                   placeholder="输入账号名称或 ID 进行搜索"
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
                :class="{ selected: account.id === accountForm.id && accountFormMode === 'edit' }"
                type="button"
                @click="editAccount(account.id)"
              >
                <span class="account-row-main">
                  <strong>{{ account.name }}</strong>
                  <small>{{ account.id }}</small>
                </span>
                <span class="account-row-desc">{{ account.description || "未填写账号说明" }}</span>
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
                  <h3>{{ accountForm.id || "请选择账号" }}</h3>
                </div>
                <button
                  class="primary-action compact"
                  type="button"
                  @click="closeAccountManager(); openSystemConfigManager()"
                >
                  <Settings :size="16" />
                  <span>打开全局配置</span>
                </button>
              </header>

              <section class="form-section strategy-section">
                <div class="section-title">
                  <h4>账号策略配置</h4>
                  <span>这些字段将影响选题筛选、素材判断和最终口吻</span>
                </div>
                <div class="form-grid">
                <label>
                  <span>账号 ID</span>
                  <input
                    ref="accountNameInput"
                    v-model="accountForm.id"
                    :disabled="accountFormMode === 'edit'"
                    autocomplete="off"
                    placeholder="finance-insight"
                  />
                  <p class="form-hint" :class="{ error: accountIdConflict }">
                  {{ accountIdConflict ? '账号 ID 已存在，请填写新的 ID。' : '建议使用小写英文，例如 finance-insight' }}
                  </p>
                </label>
                  <label>
                    <span>账号名称</span>
                    <input v-model="accountForm.name" autocomplete="off" placeholder="例如：财经洞察账号" />
                  </label>
                </div>
                <label>
                  <span>账号说明</span>
                  <textarea v-model="accountForm.description" rows="2" placeholder="简要说明账号定位和边界" />
                </label>
                <label>
                  <span>账号受众</span>
                  <textarea v-model="accountForm.audience" rows="2" placeholder="描述目标受众画像与应用场景" />
                </label>
                <label>
                  <span>选题方向</span>
                  <textarea v-model="accountForm.preferred_directions" rows="4" placeholder="如：AI 工具趋势、AI 与职场效率" />
                </label>
                <label>
                  <span>内容边界</span>
                  <textarea v-model="accountForm.boundaries" rows="4" placeholder="界定不触发争议的禁区，例如：政治、抄袭" />
                </label>
                <label>
                  <span>爆款素材</span>
                  <textarea v-model="accountForm.viral_patterns" rows="4" placeholder="可填写爆款标题结构、开篇钩子、结尾 CTA" />
                </label>
                <label>
                  <span>风格提示词</span>
                  <textarea
                    v-model="accountForm.style_prompt"
                    rows="5"
                    placeholder="例如：轻松口吻、结构清晰、给出可执行建议"
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
    <div v-if="systemConfigManagerOpen" class="modal-backdrop" @click.self="closeSystemConfigManager">
      <section class="account-manager system-manager" role="dialog" aria-modal="true" aria-labelledby="system-config-title">
        <header class="manager-header">
          <div>
            <h2 id="system-config-title">全局配置</h2>
            <p>统一管理热点来源、筛题规则与评分提示词。账号策略不会重复定义这些字段。</p>
          </div>
          <span class="manager-count">系统共享配置</span>
          <button class="icon-action" type="button" aria-label="关闭全局配置" @click="closeSystemConfigManager">
            <X :size="18" />
          </button>
        </header>

        <form class="account-form system-form" @submit.prevent="saveSystemConfig">
           <p v-if="systemConfigLoading" class="form-loading">系统配置加载中...</p>
          <template v-else>
            <section class="system-config-grid">
              <aside class="system-source-panel">
                <div class="section-title">
                  <h4>热点来源</h4>
                  <span>未选中的平台不会参与抓取</span>
                </div>
                <div class="platform-chip-wrap">
                  <button
                    v-for="platform in platformOptions"
                    :key="platform.id"
                    type="button"
                    class="chip-button"
                    :class="{ active: isSystemPlatformSelected(platform.id) }"
                    @click="toggleSystemPlatform(platform.id)"
                  >
                    {{ platform.label }}
                  </button>
                </div>
              </aside>

              <div class="system-prompt-stack">
                <section class="prompt-panel">
                  <div class="section-title">
                    <h4>选题过滤提示词</h4>
                    <span>决定哪些热点能进入候选池</span>
                  </div>
                  <textarea
                    v-model="systemConfigForm.topic_filter_prompt"
                    rows="8"
                    placeholder='例如：输入 JSON 格式，如：{"include":["AI"],"exclude":["色情","政治"]}'
                  />
                  <div class="prompt-panel-footer">
                    <span>{{ systemConfigForm.topic_filter_prompt.length }} 字</span>
                    <button
                      type="button"
                      class="chip-button compact"
                      @click="applyFilterPromptPreset('')"
                    >
                      重置筛选提示词
                    </button>
                  </div>
                </section>

                <section class="prompt-panel">
                  <div class="section-title">
                    <h4>选题评分提示词</h4>
                    <span>定义评分维度、排序逻辑和输出约束</span>
                  </div>
                  <textarea
                    v-model="systemConfigForm.topic_scoring_prompt"
                    rows="5"
                    placeholder="例如：按相关性、用户价值、可写性、时效性给出1-100分，输出 JSON 数组。"
                  />
                  <div class="prompt-panel-footer">
                    <span>{{ systemConfigForm.topic_scoring_prompt.length }} 字</span>
                  </div>
                </section>
              </div>
            </section>
            <p v-if="systemConfigMessage" class="form-message" role="status" aria-live="polite">{{ systemConfigMessage }}</p>
            <div class="form-actions">
              <button
                class="danger-action"
                type="button"
                :disabled="systemConfigSaving"
                @click="resetSystemConfigToDefault"
              >
                <RotateCcw :size="16" />
                <span>恢复默认</span>
              </button>
              <button
                class="primary-action compact"
                type="button"
                :disabled="systemConfigSaving || !canSaveSystemConfig"
                @click="saveSystemConfig"
              >
                <LoaderCircle v-if="systemConfigSaving" :size="16" class="spin" />
                <Save v-else :size="16" />
                <span>{{ systemConfigSaving ? "保存中..." : "保存系统配置" }}</span>
              </button>
            </div>
          </template>
        </form>
      </section>
    </div>
    <div v-if="artifactPreviewOpen && artifactPreview.title" class="modal-backdrop" @click.self="closeArtifactPreview">
      <section class="research-preview-modal" role="dialog" aria-modal="true" aria-labelledby="artifact-preview-title">
        <header class="manager-header">
          <div>
            <h2 id="artifact-preview-title">产出内容预览（Markdown）</h2>
            <p>{{ artifactPreview.title }}</p>
          </div>
          <button class="icon-action" type="button" aria-label="关闭内容预览" @click="closeArtifactPreview">
            <X :size="18" />
          </button>
        </header>
        <div class="research-preview-body">
          <div
            class="research-pack-rendered"
            v-html="artifactPreviewRenderedContent"
          ></div>
        </div>
      </section>
    </div>
  </main>
</template>












