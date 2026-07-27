<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Clock3,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  ServerCog
} from '@lucide/vue';
import AdminShell from '../components/AdminShell.vue';
import { createModelConfigRequestGuard } from './modelConfigRequestGuard';
import {
  ApiError,
  adminApi,
  type AdminModelConfiguration,
  type AdminModelProbePayload,
  type AdminModelUpdatePayload
} from '../services/api';

const active = ref<AdminModelConfiguration | null>(null);
const baseUrl = ref('');
const apiKey = ref('');
const modelName = ref('');
const temperature = ref<number | null>(0.2);
const temperatureMode = ref<'auto' | 'custom'>('auto');
const contextWindowTokens = ref<number | null>(32_000);
const chatMaxTokens = ref<number | null>(8_000);
const structuredMaxTokens = ref<number | null>(8_000);
const models = ref<string[]>([]);
const modelsTruncated = ref(false);
const latencyMs = ref<number | null>(null);
const loading = ref(true);
const probing = ref(false);
const saving = ref(false);
const initialLoadFailed = ref(false);
const versionSyncFailed = ref(false);
const errorMessage = ref('');
const errorKind = ref<'load' | 'probe' | 'save' | ''>('');
const successMessage = ref('');
let applyingServerState = false;

const readConnectionDraft = () => ({
  baseUrl: baseUrl.value,
  apiKey: apiKey.value,
  modelName: modelName.value
});
const readSaveDraft = () => ({
  ...readConnectionDraft(),
  temperature: selectedTemperature.value,
  contextWindowTokens: contextWindowTokens.value,
  chatMaxTokens: chatMaxTokens.value,
  structuredMaxTokens: structuredMaxTokens.value
});
const probeGuard = createModelConfigRequestGuard(readConnectionDraft);
const saveGuard = createModelConfigRequestGuard(readSaveDraft);

const configured = computed(() => Boolean(active.value?.configured));
const expectedVersion = computed(() => active.value?.version ?? 0);
const selectedTemperature = computed(() => (
  temperatureMode.value === 'auto' ? null : temperature.value
));
const temperatureError = computed(() => {
  if (temperatureMode.value === 'auto') return '';
  const value = temperature.value;
  if (typeof value !== 'number' || !Number.isFinite(value)) return '请输入 0 到 2 之间的数字。';
  if (value < 0 || value > 2) return 'Temperature 必须在 0 到 2 之间。';
  return '';
});
const contextWindowError = computed(() => {
  const value = contextWindowTokens.value;
  if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) return '上下文窗口必须是正整数。';
  return '';
});
const chatMaxError = computed(() => {
  const value = chatMaxTokens.value;
  if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) return '对话最大输出必须是正整数。';
  if (!contextWindowError.value && value >= contextWindowTokens.value!) return '对话最大输出必须小于上下文窗口。';
  return '';
});
const structuredMaxError = computed(() => {
  const value = structuredMaxTokens.value;
  if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) return '结构化输出必须是正整数。';
  if (!contextWindowError.value && value >= contextWindowTokens.value!) return '结构化输出必须小于上下文窗口。';
  return '';
});
const outputTokenMax = computed(() => (
  !contextWindowError.value && contextWindowTokens.value !== null
    ? Math.max(1, contextWindowTokens.value - 1)
    : undefined
));
const canProbe = computed(() => baseUrl.value.trim().length > 0 && !initialLoadFailed.value && !loading.value && !probing.value && !saving.value);
const canSave = computed(() => (
  canProbe.value
  && !versionSyncFailed.value
  && modelName.value.trim().length > 0
  && (configured.value || apiKey.value.trim().length > 0)
  && !temperatureError.value
  && !contextWindowError.value
  && !chatMaxError.value
  && !structuredMaxError.value
));

const errorCopy: Record<string, string> = {
  MODEL_CREDENTIALS_REQUIRED: '首次配置必须填写 API Key。',
  MODEL_ENDPOINT_FORBIDDEN: '这个模型地址不符合安全规则，请检查协议、地址和网络范围。',
  MODEL_AUTH_FAILED: '模型服务拒绝了当前凭证，请检查 API Key。',
  MODEL_NOT_FOUND: '模型服务无法使用这个模型 ID，请刷新候选或检查自定义值。',
  MODEL_PROVIDER_UNREACHABLE: '暂时无法连接模型服务，请检查地址和网络。',
  MODEL_CAPABILITIES_UNSUPPORTED: '该模型端点不支持原生工具调用或 JSON Schema 结构化输出，无法用于 ContentAI。',
  MODEL_PROBE_FAILED: '模型服务返回了无法验证的结果，请稍后重试。',
  MODEL_CONFIG_PERSISTENCE_FAILED: '模型配置保存失败，请稍后重试。',
  MODEL_CONFIG_CHANGED: '配置已被其他管理员更新，已刷新为最新版本，请核对后重试。'
};

function formatError(value: unknown) {
  if (value instanceof ApiError) return errorCopy[value.code] ?? value.message;
  return value instanceof Error ? value.message : String(value || '请求失败');
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
}

function formatRuntimeNumber(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? new Intl.NumberFormat('zh-CN').format(value) : '—';
}

function formatTemperature(value: number | null | undefined) {
  return value === null
    ? '自动（由模型决定）'
    : formatRuntimeNumber(value);
}

function setTemperatureMode(mode: 'auto' | 'custom') {
  temperatureMode.value = mode;
  if (mode === 'custom' && (typeof temperature.value !== 'number' || !Number.isFinite(temperature.value))) {
    temperature.value = 0.2;
  }
}

function payload(includeModel: boolean): AdminModelProbePayload {
  return {
    base_url: baseUrl.value.trim(),
    ...(apiKey.value.trim() ? { api_key: apiKey.value } : {}),
    ...(includeModel && modelName.value.trim() ? { model_name: modelName.value.trim() } : {})
  };
}

async function loadConfiguration(options: { preserveForm?: boolean; background?: boolean; propagate?: boolean } = {}) {
  if (!options.background) {
    loading.value = true;
    errorMessage.value = '';
    errorKind.value = '';
  }
  try {
    const result = await adminApi.modelConfig();
    active.value = result;
    initialLoadFailed.value = false;
    versionSyncFailed.value = false;
    if (!options.preserveForm) {
      applyingServerState = true;
      try {
        baseUrl.value = result.base_url ?? '';
        modelName.value = result.model_name ?? '';
        apiKey.value = '';
        temperatureMode.value = result.configured && result.temperature !== null ? 'custom' : 'auto';
        if (result.configured && result.temperature !== null) {
          temperature.value = result.temperature;
        } else if (typeof temperature.value !== 'number' || !Number.isFinite(temperature.value)) {
          temperature.value = 0.2;
        }
        contextWindowTokens.value = result.configured ? result.context_window_tokens : 32_000;
        chatMaxTokens.value = result.configured ? result.chat_max_tokens : 8_000;
        structuredMaxTokens.value = result.configured ? result.structured_max_tokens : 8_000;
        models.value = result.model_name ? [result.model_name] : [];
      } finally {
        applyingServerState = false;
      }
    }
  } catch (value) {
    if (!options.background) {
      initialLoadFailed.value = true;
      errorMessage.value = formatError(value);
      errorKind.value = 'load';
    }
    if (options.propagate) throw value;
  } finally {
    if (!options.background) loading.value = false;
  }
}

async function retryInitialLoad() {
  await loadConfiguration();
}

async function retryVersionSync() {
  saving.value = true;
  errorMessage.value = '';
  errorKind.value = '';
  try {
    await loadConfiguration({ preserveForm: true, background: true, propagate: true });
    errorMessage.value = errorCopy.MODEL_CONFIG_CHANGED;
    errorKind.value = 'save';
  } catch {
    versionSyncFailed.value = true;
    errorMessage.value = '配置已变化，但暂时无法刷新最新版本；请重新同步后再保存。';
    errorKind.value = 'save';
  } finally {
    saving.value = false;
  }
}

async function runProbe(includeModel: boolean) {
  if (!canProbe.value) return;
  const requestPayload = payload(includeModel);
  const request = probeGuard.begin();
  probing.value = true;
  errorMessage.value = '';
  errorKind.value = '';
  successMessage.value = '';
  try {
    const result = await adminApi.probeModelConfig(requestPayload);
    if (!probeGuard.isCurrent(request)) return;
    applyingServerState = true;
    try {
      models.value = result.models;
      modelsTruncated.value = result.models_truncated;
      if (!includeModel && !modelName.value && result.models.length) modelName.value = result.models[0];
      latencyMs.value = result.latency_ms;
      successMessage.value = includeModel
        ? result.model_validated
          ? `连接与模型验证成功，耗时约 ${result.latency_ms} ms。`
          : `连接成功，但当前模型尚未完成推理验证，耗时约 ${result.latency_ms} ms。`
        : result.models.length
          ? `已刷新 ${result.models.length} 个可用模型。`
          : '连接成功，但服务没有返回模型候选；仍可填写自定义模型 ID。';
    } finally {
      applyingServerState = false;
    }
  } catch (value) {
    if (probeGuard.isCurrent(request)) {
      errorMessage.value = formatError(value);
      errorKind.value = 'probe';
    }
  } finally {
    if (probeGuard.isLatest(request)) probing.value = false;
  }
}

async function saveConfiguration() {
  if (!canSave.value) return;
  const runtimeValues = [
    contextWindowTokens.value,
    chatMaxTokens.value,
    structuredMaxTokens.value
  ];
  if (!runtimeValues.every((value) => typeof value === 'number' && Number.isFinite(value))) return;
  const requestPayload: AdminModelUpdatePayload = {
    ...payload(true),
    model_name: modelName.value.trim(),
    expected_version: expectedVersion.value,
    temperature: selectedTemperature.value,
    context_window_tokens: contextWindowTokens.value!,
    chat_max_tokens: chatMaxTokens.value!,
    structured_max_tokens: structuredMaxTokens.value!
  };
  const request = saveGuard.begin();
  saving.value = true;
  errorMessage.value = '';
  errorKind.value = '';
  successMessage.value = '';
  try {
    const result = await adminApi.updateModelConfig(requestPayload);
    if (!saveGuard.isCurrent(request)) return;
    applyingServerState = true;
    try {
      active.value = result;
      baseUrl.value = result.base_url ?? requestPayload.base_url;
      modelName.value = result.model_name ?? requestPayload.model_name;
      apiKey.value = '';
      const savedTemperature = result.temperature === undefined
        ? requestPayload.temperature
        : result.temperature;
      temperatureMode.value = savedTemperature === null ? 'auto' : 'custom';
      if (savedTemperature !== null) temperature.value = savedTemperature;
      contextWindowTokens.value = result.context_window_tokens ?? requestPayload.context_window_tokens;
      chatMaxTokens.value = result.chat_max_tokens ?? requestPayload.chat_max_tokens;
      structuredMaxTokens.value = result.structured_max_tokens ?? requestPayload.structured_max_tokens;
      models.value = Array.from(new Set([...models.value, modelName.value])).sort();
      successMessage.value = `版本 v${result.version} 已验证并启用，仅对新 execution 生效。`;
    } finally {
      applyingServerState = false;
    }
  } catch (error) {
    if (error instanceof ApiError && error.code === 'MODEL_CONFIG_CHANGED') {
      try {
        await loadConfiguration({ preserveForm: true, background: true, propagate: true });
        if (saveGuard.isCurrent(request)) {
          errorMessage.value = errorCopy.MODEL_CONFIG_CHANGED;
          errorKind.value = 'save';
        }
      } catch {
        if (saveGuard.isCurrent(request)) {
          versionSyncFailed.value = true;
          errorMessage.value = '配置已变化，但暂时无法刷新最新版本；请重新同步后再保存。';
          errorKind.value = 'save';
        }
      }
    } else if (saveGuard.isCurrent(request)) {
      errorMessage.value = formatError(error);
      errorKind.value = 'save';
    }
  } finally {
    saving.value = false;
  }
}

watch([baseUrl, apiKey], () => {
  if (applyingServerState) return;
  probeGuard.invalidate();
  saveGuard.invalidate();
  probing.value = false;
  models.value = [];
  modelsTruncated.value = false;
  latencyMs.value = null;
  successMessage.value = '';
  if (errorKind.value === 'probe') {
    errorMessage.value = '';
    errorKind.value = '';
  }
}, { flush: 'sync' });

watch(modelName, () => {
  if (applyingServerState) return;
  probeGuard.invalidate();
  saveGuard.invalidate();
  probing.value = false;
  latencyMs.value = null;
  successMessage.value = '';
  if (errorKind.value === 'probe') {
    errorMessage.value = '';
    errorKind.value = '';
  }
}, { flush: 'sync' });

watch([temperatureMode, temperature, contextWindowTokens, chatMaxTokens, structuredMaxTokens], () => {
  if (applyingServerState) return;
  saveGuard.invalidate();
  successMessage.value = '';
}, { flush: 'sync' });

onMounted(() => void loadConfiguration());
</script>

<template>
  <AdminShell title="模型管理" description="配置全局 OpenAI 兼容端点，并为新任务选择统一模型。">
    <template #heading-status>
      <div class="admin-model-heading-status" :class="{ configured }" aria-live="polite">
        <span><Activity :size="16" />{{ initialLoadFailed ? '配置加载失败' : configured ? '服务已配置' : '等待首次配置' }}</span>
        <strong>{{ configured ? `v${active?.version}` : '—' }}</strong>
      </div>
    </template>

    <div class="model-admin-workspace" :aria-busy="loading || probing || saving">
      <section class="model-status-summary" aria-labelledby="model-status-summary-title">
        <div>
          <span><Activity :size="17" />当前生效状态</span>
          <strong id="model-status-summary-title">{{ initialLoadFailed ? '加载失败' : configured ? `v${active?.version} · ${active?.model_name}` : '尚未配置' }}</strong>
        </div>
        <p v-if="loading">正在读取当前配置…</p>
        <p v-else-if="initialLoadFailed">重新加载成功前不会开放保存。</p>
        <p v-else-if="configured">{{ active?.base_url }}</p>
        <p v-else>完成验证并保存后，新对话才能提交。</p>
        <dl v-if="!loading && !initialLoadFailed && configured" class="model-runtime-summary" aria-label="当前生效运行参数">
          <div><dt>Temperature</dt><dd>{{ formatTemperature(active?.temperature) }}</dd></div>
          <div><dt>上下文窗口</dt><dd>{{ formatRuntimeNumber(active?.context_window_tokens) }}</dd></div>
          <div><dt>对话输出</dt><dd>{{ formatRuntimeNumber(active?.chat_max_tokens) }}</dd></div>
          <div><dt>结构化输出</dt><dd>{{ formatRuntimeNumber(active?.structured_max_tokens) }}</dd></div>
        </dl>
      </section>

      <section class="model-config-panel" aria-labelledby="model-config-form-title">
        <div v-if="loading" class="model-config-skeleton" role="status" aria-label="正在加载模型配置">
          <span v-for="index in 6" :key="index"></span>
        </div>

        <div v-else-if="initialLoadFailed" class="model-empty-state model-load-error" role="alert">
          <CircleAlert :size="24" />
          <strong>无法加载当前模型配置</strong>
          <p>{{ errorMessage }}。为避免覆盖已有版本，配置表单已暂停。</p>
          <button class="model-retry-action" type="button" @click="retryInitialLoad"><RefreshCw :size="16" />重新加载</button>
        </div>

        <form v-else class="model-config-form" @submit.prevent="saveConfiguration">
          <header>
            <span class="model-panel-icon"><ServerCog :size="20" /></span>
            <div><h2 id="model-config-form-title">OpenAI 兼容配置</h2><p>保存前服务端会重新拉取模型并完成一次最小推理验证。</p></div>
          </header>

          <label class="model-field">
            <span>Base URL</span>
            <input v-model="baseUrl" type="url" inputmode="url" autocomplete="url" placeholder="https://gateway.example.com/v1" required />
            <small>填写 OpenAI API 根路径；系统不会追加 /openai 或 /anthropic。</small>
          </label>

          <label class="model-field">
            <span>API Key</span>
            <span class="model-secret-control"><KeyRound :size="16" /><input v-model="apiKey" type="password" autocomplete="new-password" :required="!configured" placeholder="sk-…" /></span>
            <small v-if="configured">已保存 {{ active?.api_key_hint }}；留空表示不更换已保存的 Key。</small>
            <small v-else>首次配置必须填写 Key。保存后只显示不可逆的非敏感提示。</small>
          </label>

          <div class="model-field">
            <label for="model-name">模型 ID</label>
            <div class="model-input-action">
              <input id="model-name" v-model="modelName" list="model-candidates" autocomplete="off" placeholder="例如 gpt-4.1-mini" required />
              <button type="button" :disabled="!canProbe" @click="runProbe(false)">
                <LoaderCircle v-if="probing" :size="16" class="spin" /><RefreshCw v-else :size="16" />刷新模型
              </button>
            </div>
            <datalist id="model-candidates"><option v-for="model in models" :key="model" :value="model" /></datalist>
            <small>可选择候选，也可直接输入自定义模型 ID。</small>
            <p v-if="!models.length && latencyMs !== null" class="model-inline-note">模型列表为空；你仍可以测试自定义模型 ID。</p>
            <p v-if="modelsTruncated" class="model-inline-note">候选较多，列表已安全截断；可直接输入未显示的模型 ID。</p>
          </div>

          <fieldset class="model-runtime-fieldset">
            <legend>运行参数</legend>
            <p class="model-runtime-intro">这些参数会随配置版本固化，只影响保存后新建的 execution。</p>
            <div class="model-runtime-grid">
              <div class="model-field">
                <label for="model-temperature">Temperature</label>
                <label class="model-temperature-mode" for="model-temperature-mode">
                  <span>Temperature 控制方式</span>
                  <select
                    id="model-temperature-mode"
                    :value="temperatureMode"
                    @change="setTemperatureMode(($event.target as HTMLSelectElement).value as 'auto' | 'custom')"
                  >
                    <option value="auto">自动（由模型决定）</option>
                    <option value="custom">自定义数值</option>
                  </select>
                </label>
                <input
                  id="model-temperature"
                  v-model.number="temperature"
                  type="number"
                  min="0"
                  max="2"
                  step="0.1"
                  inputmode="decimal"
                  :disabled="temperatureMode === 'auto'"
                  :required="temperatureMode === 'custom'"
                  :aria-invalid="Boolean(temperatureError)"
                  :aria-describedby="temperatureError ? 'model-temperature-help model-temperature-error' : 'model-temperature-help'"
                />
                <small id="model-temperature-help">自动模式不会向模型服务发送 Temperature；自定义值允许 0 到 2，包含边界。</small>
                <p v-if="temperatureError" id="model-temperature-error" class="model-field-error">{{ temperatureError }}</p>
              </div>

              <div class="model-field">
                <label for="model-context-window">上下文窗口（tokens）</label>
                <input
                  id="model-context-window"
                  v-model.number="contextWindowTokens"
                  type="number"
                  min="1"
                  step="1"
                  inputmode="numeric"
                  required
                  :aria-invalid="Boolean(contextWindowError)"
                  :aria-describedby="contextWindowError ? 'model-context-window-help model-context-window-error' : 'model-context-window-help'"
                />
                <small id="model-context-window-help">模型一次调用可使用的总 token 上限。</small>
                <p v-if="contextWindowError" id="model-context-window-error" class="model-field-error">{{ contextWindowError }}</p>
              </div>

              <div class="model-field">
                <label for="model-chat-max">对话最大输出（tokens）</label>
                <input
                  id="model-chat-max"
                  v-model.number="chatMaxTokens"
                  type="number"
                  min="1"
                  :max="outputTokenMax"
                  step="1"
                  inputmode="numeric"
                  required
                  :aria-invalid="Boolean(chatMaxError)"
                  :aria-describedby="chatMaxError ? 'model-chat-max-help model-chat-max-error' : 'model-chat-max-help'"
                />
                <small id="model-chat-max-help">普通对话的最大输出，必须小于上下文窗口。</small>
                <p v-if="chatMaxError" id="model-chat-max-error" class="model-field-error">{{ chatMaxError }}</p>
              </div>

              <div class="model-field">
                <label for="model-structured-max">结构化输出（tokens）</label>
                <input
                  id="model-structured-max"
                  v-model.number="structuredMaxTokens"
                  type="number"
                  min="1"
                  :max="outputTokenMax"
                  step="1"
                  inputmode="numeric"
                  required
                  :aria-invalid="Boolean(structuredMaxError)"
                  :aria-describedby="structuredMaxError ? 'model-structured-max-help model-structured-max-error' : 'model-structured-max-help'"
                />
                <small id="model-structured-max-help">结构化调用的最大输出，必须小于上下文窗口。</small>
                <p v-if="structuredMaxError" id="model-structured-max-error" class="model-field-error">{{ structuredMaxError }}</p>
              </div>
            </div>
          </fieldset>

          <div class="model-form-actions">
            <button class="secondary-action" type="button" :disabled="!canProbe || !modelName.trim()" @click="runProbe(true)">
              <LoaderCircle v-if="probing" :size="16" class="spin" /><Activity v-else :size="16" />测试连接
            </button>
            <button class="primary-action" type="submit" :disabled="!canSave">
              <LoaderCircle v-if="saving" :size="16" class="spin" /><Save v-else :size="16" />{{ saving ? '正在验证并保存' : '保存并启用' }}
            </button>
          </div>

          <div class="model-feedback" aria-live="polite" aria-atomic="true">
            <p v-if="errorMessage" class="form-feedback error" role="alert"><CircleAlert :size="16" />{{ errorMessage }}</p>
            <button v-if="versionSyncFailed" class="model-retry-action" type="button" :disabled="saving" @click="retryVersionSync"><RefreshCw :size="16" />重新同步版本</button>
            <p v-if="successMessage" class="form-feedback success" role="status"><CheckCircle2 :size="16" />{{ successMessage }}</p>
          </div>
        </form>
      </section>

      <details class="model-status-details">
        <summary>
          <span>查看完整生效状态</span>
          <small>版本、连接、运行参数与验证信息</small>
        </summary>
        <div class="model-status-content">
          <header><span class="model-panel-icon"><Activity :size="20" /></span><div><h2>当前生效状态</h2><p>不可变版本快照</p></div></header>
          <div v-if="loading" class="model-status-skeleton"><span v-for="index in 5" :key="index"></span></div>
          <div v-else-if="initialLoadFailed" class="model-empty-state">
            <CircleAlert :size="24" />
            <strong>状态暂不可用</strong>
            <p>重新加载成功前，系统不会把错误状态当作首次配置。</p>
          </div>
          <div v-else-if="!configured" class="model-empty-state">
            <CircleAlert :size="24" />
            <strong>尚未配置模型服务</strong>
            <p>完成连接验证并保存后，新对话才能提交。</p>
          </div>
          <template v-else>
            <div class="model-version-mark"><span>ACTIVE VERSION</span><strong>v{{ active?.version }}</strong></div>
            <dl class="model-status-list">
              <div><dt>Provider</dt><dd>OpenAI Compatible</dd></div>
              <div><dt>Base URL</dt><dd>{{ active?.base_url }}</dd></div>
              <div><dt>模型</dt><dd>{{ active?.model_name }}</dd></div>
              <div><dt>Key</dt><dd>{{ active?.api_key_hint }}</dd></div>
              <div><dt>Temperature</dt><dd>{{ formatTemperature(active?.temperature) }}</dd></div>
              <div><dt>上下文窗口</dt><dd>{{ formatRuntimeNumber(active?.context_window_tokens) }} tokens</dd></div>
              <div><dt>对话输出</dt><dd>{{ formatRuntimeNumber(active?.chat_max_tokens) }} tokens</dd></div>
              <div><dt>结构化输出</dt><dd>{{ formatRuntimeNumber(active?.structured_max_tokens) }} tokens</dd></div>
              <div><dt>验证时间</dt><dd>{{ formatDate(active?.validated_at) }}</dd></div>
              <div><dt>操作者</dt><dd>{{ active?.created_by_email || active?.created_by_user_id || '—' }}</dd></div>
            </dl>
            <div class="model-effective-note"><Clock3 :size="18" /><p><strong>仅新 execution 生效</strong><span>已排队、运行、恢复或重试的任务继续使用创建时固化的版本。</span></p></div>
          </template>
        </div>
      </details>

      <aside class="model-status-panel" aria-labelledby="model-status-title" tabindex="0">
        <header><span class="model-panel-icon"><Activity :size="20" /></span><div><h2 id="model-status-title">当前生效状态</h2><p>不可变版本快照</p></div></header>
        <div v-if="loading" class="model-status-skeleton"><span v-for="index in 5" :key="index"></span></div>
        <div v-else-if="initialLoadFailed" class="model-empty-state">
          <CircleAlert :size="24" />
          <strong>状态暂不可用</strong>
          <p>重新加载成功前，系统不会把错误状态当作首次配置。</p>
        </div>
        <div v-else-if="!configured" class="model-empty-state">
          <CircleAlert :size="24" />
          <strong>尚未配置模型服务</strong>
          <p>在左侧完成连接验证并保存后，新对话才能提交。</p>
        </div>
        <template v-else>
          <div class="model-version-mark"><span>ACTIVE VERSION</span><strong>v{{ active?.version }}</strong></div>
          <dl class="model-status-list">
            <div><dt>Provider</dt><dd>OpenAI Compatible</dd></div>
            <div><dt>Base URL</dt><dd>{{ active?.base_url }}</dd></div>
            <div><dt>模型</dt><dd>{{ active?.model_name }}</dd></div>
            <div><dt>Key</dt><dd>{{ active?.api_key_hint }}</dd></div>
            <div><dt>Temperature</dt><dd>{{ formatTemperature(active?.temperature) }}</dd></div>
            <div><dt>上下文窗口</dt><dd>{{ formatRuntimeNumber(active?.context_window_tokens) }} tokens</dd></div>
            <div><dt>对话输出</dt><dd>{{ formatRuntimeNumber(active?.chat_max_tokens) }} tokens</dd></div>
            <div><dt>结构化输出</dt><dd>{{ formatRuntimeNumber(active?.structured_max_tokens) }} tokens</dd></div>
            <div><dt>验证时间</dt><dd>{{ formatDate(active?.validated_at) }}</dd></div>
            <div><dt>操作者</dt><dd>{{ active?.created_by_email || active?.created_by_user_id || '—' }}</dd></div>
          </dl>
          <div class="model-effective-note"><Clock3 :size="18" /><p><strong>仅新 execution 生效</strong><span>已排队、运行、恢复或重试的任务继续使用创建时固化的版本。</span></p></div>
        </template>
      </aside>
    </div>
  </AdminShell>
</template>
