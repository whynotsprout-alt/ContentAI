<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  Check,
  ChevronRight,
  LoaderCircle,
  Plus,
  Save,
  Search,
  Trash2,
  X
} from '@lucide/vue';
import { ApiError, api, type AgentProfileDetail, type AgentProfilePayload } from '../services/api';
import { useWorkbenchStore } from '../stores/workbench';
import AccessibleDialog from './AccessibleDialog.vue';

type TemplateId = 'finance' | 'ai';
type SectionId = 'basic' | 'sources' | 'scoring' | 'content';
type SourceOption = { id: string; label: string };

const props = withDefaults(defineProps<{
  open: boolean;
  initialTemplate?: TemplateId | null;
}>(), { initialTemplate: null });
const emit = defineEmits<{ close: [] }>();
const store = useWorkbenchStore();

const sourceGroups: Array<{ title: string; sources: SourceOption[] }> = [
  { title: '中文商业与科技', sources: [
    { id: '36kr', label: '36Kr' }, { id: 'cls', label: '财联社' }, { id: 'eeo', label: '经济观察报' },
    { id: 'yicai', label: '第一财经' }, { id: 'huxiu', label: '虎嗅' }, { id: 'jiemian', label: '界面' },
    { id: 'tmtpost', label: '钛媒体' }, { id: 'latepost', label: '晚点' }, { id: 'qbitai', label: '量子位' },
    { id: 'leiphone', label: '雷峰网' }, { id: 'ifanr', label: '爱范儿' }, { id: 'stcn', label: '证券时报' }
  ] },
  { title: '国际资讯', sources: [
    { id: 'techcrunch', label: 'TechCrunch' }, { id: 'theverge', label: 'The Verge' },
    { id: 'ft', label: 'FT' }, { id: 'wsj', label: 'WSJ' }, { id: 'caixin', label: '财新' }
  ] },
  { title: '社交热榜与聚合', sources: [
    { id: 'douyin', label: '抖音' }, { id: 'bilibili', label: 'Bilibili' }, { id: 'xiaohongshu', label: '小红书' },
    { id: 'weibo', label: '微博' }, { id: 'aihot', label: 'AI HOT' }, { id: 'vista', label: 'Vista 看天下' }
  ] }
];
const allSources = sourceGroups.flatMap((group) => group.sources.map((source) => source.id));
const templates: Record<TemplateId, { name: string; positioning: string; scoring: string; content: string; sources: string[] }> = {
  finance: {
    name: '财经解读',
    positioning: '面向普通读者解读商业、消费与政策变化，强调真实影响和行动价值。',
    scoring: '从受众相关性、事实强度、传播潜力、差异化角度和内容风险五个维度给候选选题评分，并说明取舍。',
    content: '用准确、清晰、克制的中文完成内容。先给结论，再解释原因；区分事实、判断和不确定信息。',
    sources: allSources.filter((id) => !['qbitai', 'leiphone', 'techcrunch', 'theverge'].includes(id))
  },
  ai: {
    name: 'AI 资讯',
    positioning: '跟踪 AI 产品、技术与行业应用，用可验证事实解释它对真实工作和生活的影响。',
    scoring: '重点评估新鲜度、可验证性、使用价值、讨论空间和过度营销风险，优先选择有可靠来源的主题。',
    content: '避免堆砌术语和空泛赞美。用具体场景解释技术价值，明确引用来源，并标注仍待验证的部分。',
    sources: allSources
  }
};

const search = ref('');
const activeSection = ref<SectionId>('basic');
const compactView = ref<'directory' | 'editor'>('directory');
const compactLayout = ref(true);
const mode = ref<'create' | 'edit'>('create');
const editingId = ref('');
const originalSnapshot = ref('');
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const notice = ref('');
const confirmClose = ref(false);
const confirmDelete = ref(false);
const discardTarget = ref<'close' | 'directory'>('close');
const touched = ref(false);
const form = ref({ name: '', positioning: '', scoring: '', content: '', sources: [] as string[] });

const sections: Array<{ id: SectionId; label: string }> = [
  { id: 'basic', label: '基础信息' }, { id: 'sources', label: '热点来源' }, { id: 'scoring', label: '评分规则' },
  { id: 'content', label: '内容规则' }
];
const filteredAgents = computed(() => {
  const term = search.value.trim().toLocaleLowerCase('zh-CN');
  return store.agents.filter((agent) => !term || `${agent.name} ${agent.description}`.toLocaleLowerCase('zh-CN').includes(term));
});
const fieldErrors = computed(() => ({
  name: form.value.name.trim() ? '' : '请填写账号名称',
  positioning: form.value.positioning.trim() ? '' : '请说明账号定位',
  scoring: form.value.scoring.trim() ? '' : '请填写选题评分规则',
  content: form.value.content.trim() ? '' : '请填写内容表达规则',
  sources: form.value.sources.length ? '' : '至少选择一个热点来源'
}));
const valid = computed(() => Object.values(fieldErrors.value).every((value) => !value));
const snapshot = computed(() => JSON.stringify(form.value));
const dirty = computed(() => snapshot.value !== originalSnapshot.value);
let compactLayoutQuery: MediaQueryList | null = null;

function syncCompactLayout(event?: MediaQueryListEvent) {
  compactLayout.value = event?.matches ?? compactLayoutQuery?.matches ?? true;
}

function resetForm(template?: TemplateId | null) {
  const source = template ? templates[template] : null;
  form.value = source
    ? { name: source.name, positioning: source.positioning, scoring: source.scoring, content: source.content, sources: [...source.sources] }
    : { name: '', positioning: '', scoring: '', content: '', sources: [...allSources] };
  mode.value = 'create';
  editingId.value = '';
  activeSection.value = 'basic';
  error.value = '';
  notice.value = '';
  touched.value = false;
  originalSnapshot.value = JSON.stringify(form.value);
}

function startNewAgent() {
  if (dirty.value) {
    error.value = '当前配置尚未保存，请先保存或关闭后再新建。';
    return;
  }
  resetForm();
  compactView.value = 'editor';
  void focusActiveTab();
}

function selectedSources(agent: AgentProfileDetail) {
  return agent.current_version?.hotspot_sources ?? [];
}

async function edit(agentId: string, navigate = true) {
  if (dirty.value && originalSnapshot.value && editingId.value !== agentId) {
    error.value = '当前配置尚未保存，请先保存或新建后再切换。';
    return;
  }
  if (navigate) compactView.value = 'editor';
  loading.value = true;
  error.value = '';
  notice.value = '';
  try {
    const agent = await api.agent(agentId);
    form.value = {
      name: agent.name,
      positioning: agent.description,
      scoring: agent.current_version?.topic_scoring_prompt ?? '',
      content: agent.current_version?.content_prompt ?? '',
      sources: selectedSources(agent)
    };
    mode.value = 'edit';
    editingId.value = agent.id;
    touched.value = false;
    originalSnapshot.value = JSON.stringify(form.value);
    if (navigate) void focusActiveTab();
  } catch (value) {
    error.value = value instanceof ApiError ? value.message : String(value);
  } finally {
    loading.value = false;
  }
}

async function focusActiveTab() {
  await nextTick();
  document.getElementById(`manager-tab-${activeSection.value}`)?.focus();
}

function onSectionKeydown(event: KeyboardEvent, sectionId: SectionId) {
  const current = sections.findIndex((section) => section.id === sectionId);
  let target = current;
  switch (event.key) {
    case 'ArrowRight':
      if (!compactLayout.value) return;
      target = (current + 1) % sections.length;
      break;
    case 'ArrowDown':
      if (compactLayout.value) return;
      target = (current + 1) % sections.length;
      break;
    case 'ArrowLeft':
      if (!compactLayout.value) return;
      target = (current - 1 + sections.length) % sections.length;
      break;
    case 'ArrowUp':
      if (compactLayout.value) return;
      target = (current - 1 + sections.length) % sections.length;
      break;
    case 'Home':
      target = 0;
      break;
    case 'End':
      target = sections.length - 1;
      break;
    default:
      return;
  }
  event.preventDefault();
  activeSection.value = sections[target].id;
  void focusActiveTab();
}

function toggleSource(sourceId: string) {
  form.value.sources = form.value.sources.includes(sourceId)
    ? form.value.sources.filter((id) => id !== sourceId)
    : [...form.value.sources, sourceId];
}

function payload(): AgentProfilePayload {
  return {
    name: form.value.name.trim(), description: form.value.positioning.trim(),
    topic_scoring_prompt: form.value.scoring.trim(), content_prompt: form.value.content.trim(),
    hotspot_sources: form.value.sources
  };
}

async function save() {
  touched.value = true;
  error.value = '';
  notice.value = '';
  if (!valid.value) {
    error.value = '请先修正标记的字段。';
    const firstInvalid = (Object.keys(fieldErrors.value) as Array<keyof typeof fieldErrors.value>).find((key) => fieldErrors.value[key]);
    if (firstInvalid === 'sources') activeSection.value = 'sources';
    else if (firstInvalid === 'scoring') activeSection.value = 'scoring';
    else if (firstInvalid === 'content') activeSection.value = 'content';
    else activeSection.value = 'basic';
    return;
  }
  saving.value = true;
  try {
    const body = payload();
    let agent: AgentProfileDetail;
    if (mode.value === 'create') {
      agent = await store.createAgent(body);
    } else {
      await store.updateAgent(editingId.value, {
        name: body.name, description: body.description
      });
      agent = await store.createAgentVersion(editingId.value, {
        topic_scoring_prompt: body.topic_scoring_prompt,
        content_prompt: body.content_prompt,
        hotspot_sources: body.hotspot_sources
      });
    }
    mode.value = 'edit';
    editingId.value = agent.id;
    originalSnapshot.value = JSON.stringify(form.value);
    notice.value = '内容账号已保存，并已切换到它的会话。';
  } catch (value) {
    error.value = value instanceof ApiError ? value.message : String(value);
  } finally {
    saving.value = false;
  }
}

async function removeAgent() {
  if (!editingId.value) return;
  saving.value = true;
  error.value = '';
  try {
    await store.deleteAgent(editingId.value);
    confirmDelete.value = false;
    if (store.agentId) await edit(store.agentId);
    else resetForm();
    notice.value = '内容账号已删除。';
  } catch (value) {
    error.value = value instanceof ApiError ? value.message : String(value);
  } finally {
    saving.value = false;
  }
}

function requestClose() {
  discardTarget.value = 'close';
  if (dirty.value) confirmClose.value = true;
  else emit('close');
}

function requestDirectory() {
  discardTarget.value = 'directory';
  if (dirty.value) {
    confirmClose.value = true;
    return;
  }
  compactView.value = 'directory';
}

function discardChanges() {
  confirmClose.value = false;
  if (discardTarget.value === 'close') {
    emit('close');
    return;
  }
  compactView.value = 'directory';
  if (store.agentId) void edit(store.agentId, false);
  else resetForm();
}

watch(() => props.open, (open) => {
  if (!open) return;
  confirmClose.value = false;
  confirmDelete.value = false;
  discardTarget.value = 'close';
  activeSection.value = 'basic';
  compactView.value = props.initialTemplate ? 'editor' : 'directory';
  if (props.initialTemplate) resetForm(props.initialTemplate);
  else if (store.agentId) void edit(store.agentId, false);
  else resetForm();
  void nextTick(() => { originalSnapshot.value = JSON.stringify(form.value); });
});

onMounted(() => {
  compactLayoutQuery = window.matchMedia('(max-width: 1279px)');
  syncCompactLayout();
  compactLayoutQuery.addEventListener('change', syncCompactLayout);
});

onBeforeUnmount(() => {
  compactLayoutQuery?.removeEventListener('change', syncCompactLayout);
});
</script>

<template>
  <AccessibleDialog v-if="open" title-id="agent-manager-title" variant="fullscreen" :busy="saving" :close-on-backdrop="false" @close="requestClose">
    <div class="agent-manager">
      <header class="manager-header">
        <div><span class="section-kicker"><Bot :size="15" /> 内容账号</span><h2 id="agent-manager-title">配置你的 ContentAI</h2><p>定义来源、选题判断和内容表达；保存后配置会参与后续对话。</p></div>
        <div class="manager-header-actions">
          <span v-if="dirty" class="dirty-indicator"><span></span>未保存</span>
          <button class="icon-action" type="button" aria-label="关闭内容账号管理" :disabled="saving" @click="requestClose"><X :size="19" /></button>
        </div>
      </header>

      <div class="manager-layout" :data-manager-view="compactView">
        <aside class="agent-directory" aria-label="内容账号目录">
          <label class="compact-search"><Search :size="15" /><input v-model="search" type="search" placeholder="搜索内容账号" aria-label="搜索内容账号" /></label>
          <button class="new-agent-button" type="button" @click="startNewAgent"><Plus :size="16" /> 新建内容账号</button>
          <button
            v-for="agent in filteredAgents" :key="agent.id" type="button" class="agent-directory-row"
            :class="{ active: agent.id === editingId }"
            :aria-current="agent.id === editingId ? 'page' : undefined"
            @click="edit(agent.id)"
          >
            <span><strong>{{ agent.name }}</strong><small>{{ agent.description || '未填写账号定位' }}</small></span><ChevronRight :size="15" />
          </button>
          <p v-if="!filteredAgents.length" class="directory-empty">没有匹配的内容账号。</p>
        </aside>

        <div class="manager-sections">
          <button class="manager-back-button" type="button" @click="requestDirectory">
            <ArrowLeft :size="16" /> 返回账号目录
          </button>
          <nav class="manager-section-tabs" aria-label="配置分区" role="tablist" :aria-orientation="compactLayout ? 'horizontal' : 'vertical'">
            <button
              v-for="section in sections"
              :id="`manager-tab-${section.id}`"
              :key="section.id"
              type="button"
              role="tab"
              :class="{ active: activeSection === section.id }"
              :aria-controls="`manager-panel-${section.id}`"
              :aria-selected="activeSection === section.id"
              :tabindex="activeSection === section.id ? 0 : -1"
              @click="activeSection = section.id"
              @keydown="onSectionKeydown($event, section.id)"
            >
              {{ section.label }}
              <span v-if="touched && fieldErrors[section.id === 'basic' ? 'name' : section.id === 'sources' ? 'sources' : section.id === 'scoring' ? 'scoring' : section.id === 'content' ? 'content' : 'name']" class="section-error"></span>
            </button>
          </nav>
        </div>

        <form class="agent-editor" aria-label="内容账号配置编辑器" :aria-busy="loading || saving" @submit.prevent="save">
          <div v-if="loading" class="editor-loading" role="status"><LoaderCircle :size="20" class="spin" /> 正在加载配置</div>
          <template v-else>
            <section id="manager-panel-basic" class="editor-section" role="tabpanel" aria-labelledby="manager-tab-basic" :hidden="activeSection !== 'basic'" tabindex="0">
              <span class="form-badge">{{ mode === 'create' ? '新建账号' : '基础信息' }}</span>
              <h3>让 Agent 理解你的内容边界</h3>
              <p>用受众和内容价值描述定位，避免只写宽泛行业词。</p>
              <label class="field-block"><span>账号名称</span><input v-model="form.name" type="text" maxlength="80" placeholder="例如：高百烈说财经" /><small v-if="touched && fieldErrors.name" class="field-error">{{ fieldErrors.name }}</small></label>
              <label class="field-block"><span>账号定位</span><textarea v-model="form.positioning" class="positioning-editor" rows="9" placeholder="服务谁、关注什么、提供什么独特价值" /><small v-if="touched && fieldErrors.positioning" class="field-error">{{ fieldErrors.positioning }}</small></label>
            </section>

            <section id="manager-panel-sources" class="editor-section" role="tabpanel" aria-labelledby="manager-tab-sources" :hidden="activeSection !== 'sources'" tabindex="0">
              <span class="form-badge">热点来源</span><h3>选择信号来源</h3><p>已选择 {{ form.sources.length }} / {{ allSources.length }} 个来源。这里只控制来源偏好，不表示连通性状态。</p>
              <div class="source-groups">
                <fieldset v-for="group in sourceGroups" :key="group.title" class="source-group"><legend>{{ group.title }}</legend><div class="source-grid">
                  <button v-for="source in group.sources" :key="source.id" type="button" :aria-pressed="form.sources.includes(source.id)" :class="{ selected: form.sources.includes(source.id) }" @click="toggleSource(source.id)"><Check v-if="form.sources.includes(source.id)" :size="14" />{{ source.label }}</button>
                </div></fieldset>
              </div><small v-if="touched && fieldErrors.sources" class="field-error">{{ fieldErrors.sources }}</small>
            </section>

            <section id="manager-panel-scoring" class="editor-section" role="tabpanel" aria-labelledby="manager-tab-scoring" :hidden="activeSection !== 'scoring'" tabindex="0">
              <span class="form-badge">评分规则</span><h3>定义什么值得做</h3><p>描述判断维度与取舍逻辑，输出仍会作为普通 Assistant 消息出现在对话中。</p>
              <label class="field-block"><span>选题评分提示词</span><textarea v-model="form.scoring" class="prompt-editor" rows="18" /><small v-if="touched && fieldErrors.scoring" class="field-error">{{ fieldErrors.scoring }}</small></label>
            </section>

            <section id="manager-panel-content" class="editor-section" role="tabpanel" aria-labelledby="manager-tab-content" :hidden="activeSection !== 'content'" tabindex="0">
              <span class="form-badge">内容规则</span><h3>定义最终表达</h3><p>写清结构、语气、事实边界和引用要求，避免塞入具体某一次任务。</p>
              <label class="field-block"><span>内容生成提示词</span><textarea v-model="form.content" class="prompt-editor" rows="18" /><small v-if="touched && fieldErrors.content" class="field-error">{{ fieldErrors.content }}</small></label>
            </section>

          </template>

          <footer class="editor-footer">
            <div><p v-if="error" class="form-feedback error" role="alert">{{ error }}</p><p v-if="notice" class="form-feedback success" role="status">{{ notice }}</p></div>
            <div class="editor-actions">
              <button v-if="mode === 'edit'" class="danger-button" type="button" :disabled="saving" @click="confirmDelete = true"><Trash2 :size="16" /> 删除</button>
              <button class="primary-action" type="submit" :disabled="saving"><LoaderCircle v-if="saving" :size="16" class="spin" /><Save v-else :size="16" />{{ saving ? '保存中' : '保存内容账号' }}</button>
            </div>
          </footer>
        </form>
      </div>

      <AccessibleDialog
        v-if="confirmClose || confirmDelete"
        title-id="manager-confirm-title"
        :busy="saving"
        @close="confirmClose = false; confirmDelete = false"
      >
        <div class="manager-confirm-card">
          <AlertTriangle :size="22" /><h3 id="manager-confirm-title">{{ confirmDelete ? '删除这个内容账号？' : '放弃未保存的更改？' }}</h3>
          <p>{{ confirmDelete ? '删除后不能用于新的对话，且无法撤销。' : '刚才修改的配置不会被保留。' }}</p>
          <div><button type="button" @click="confirmClose = false; confirmDelete = false">取消</button><button class="danger-button" type="button" @click="confirmDelete ? removeAgent() : discardChanges()">{{ confirmDelete ? '确认删除' : discardTarget === 'directory' ? '放弃并返回目录' : '放弃并关闭' }}</button></div>
        </div>
      </AccessibleDialog>
    </div>
  </AccessibleDialog>
</template>
