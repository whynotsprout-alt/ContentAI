<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { Check, Copy, LoaderCircle, MessageSquareText, Play, Square, Sparkles } from '@lucide/vue';
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import type { RunLifecycle, WorkbenchMessage } from '../stores/workbench';

const props = defineProps<{
  messages: WorkbenchMessage[];
  lifecycle: RunLifecycle;
  canSubmit: boolean;
  canResume: boolean;
  interruptSummary: string;
  hasAgent: boolean;
  switchingAgent: boolean;
  submitMessage: (message: string) => Promise<boolean>;
  resumeRun: (message: string) => Promise<boolean>;
  cancelRun: () => Promise<void>;
}>();

const emit = defineEmits<{ createAgent: [template: 'finance' | 'ai'] }>();
const prompt = ref('');
const resumePrompt = ref('');
const promptError = ref('');
const resumeSubmitting = ref(false);
const copiedIndex = ref(-1);
const expanded = ref(new Set<number>());
const chatStream = ref<HTMLElement | null>(null);
const promptInput = ref<HTMLTextAreaElement | null>(null);
const announcement = ref('');

const markdown = new MarkdownIt({ breaks: true, html: false, linkify: true });
const defaultLinkOpen = markdown.renderer.rules.link_open
  ?? ((tokens, index, options, _env, self) => self.renderToken(tokens, index, options));
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  const token = tokens[index];
  const hrefIndex = token.attrIndex('href');
  const href = hrefIndex >= 0 ? token.attrs?.[hrefIndex]?.[1] ?? '' : '';
  token.attrSet('target', '_blank');
  token.attrSet('rel', 'noopener noreferrer nofollow');
  try {
    token.attrSet('data-domain', new URL(href).hostname.replace(/^www\./, ''));
  } catch {
    token.attrSet('data-domain', '外部链接');
  }
  return defaultLinkOpen(tokens, index, options, env, self);
};

const isActive = computed(() => ['queued', 'running', 'reconnecting', 'cancelling'].includes(props.lifecycle));
const sendDisabled = computed(() => !prompt.value.trim() || !props.canSubmit || props.switchingAgent);
const lastMessageContent = computed(() =>
  props.messages.length ? props.messages[props.messages.length - 1]?.content ?? '' : ''
);

function renderMessage(message: WorkbenchMessage) {
  const html = message.message_type === 'markdown'
    ? markdown.render(message.content)
    : markdown.utils.escapeHtml(message.content).replace(/\n/g, '<br>');
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}

function isLong(message: WorkbenchMessage) {
  return message.role === 'assistant' && message.content.length > 1800;
}

function toggleExpanded(index: number) {
  const next = new Set(expanded.value);
  if (next.has(index)) next.delete(index);
  else next.add(index);
  expanded.value = next;
}

async function copyMessage(message: WorkbenchMessage, index: number) {
  await navigator.clipboard.writeText(message.content);
  copiedIndex.value = index;
  window.setTimeout(() => { if (copiedIndex.value === index) copiedIndex.value = -1; }, 1600);
}

async function send() {
  const value = prompt.value.trim();
  promptError.value = '';
  if (!value) {
    promptError.value = '请输入具体任务后再发送。';
    promptInput.value?.focus();
    return;
  }
  if (!props.hasAgent) {
    promptError.value = '请先创建一个内容账号。';
    return;
  }
  if (!props.canSubmit) return;
  const accepted = await props.submitMessage(value);
  if (accepted) prompt.value = '';
  await nextTick();
  promptInput.value?.focus();
}

async function resume() {
  const value = resumePrompt.value.trim();
  if (!value || resumeSubmitting.value) return;
  resumeSubmitting.value = true;
  try {
    const accepted = await props.resumeRun(value);
    if (accepted) resumePrompt.value = '';
  } finally {
    resumeSubmitting.value = false;
  }
}

function useSuggestion(value: string) {
  prompt.value = value;
  void nextTick(() => promptInput.value?.focus());
}

watch([() => props.messages.length, lastMessageContent], () => {
  void nextTick(() => {
    if (chatStream.value) chatStream.value.scrollTop = chatStream.value.scrollHeight;
  });
});

watch(() => props.lifecycle, (next, previous) => {
  if (next === 'completed' && previous !== 'completed') announcement.value = 'Agent 回复完成';
  else if (next === 'failed') announcement.value = '本次执行失败';
  else if (next === 'waiting_input') announcement.value = 'Agent 正在等待你的确认';
});
</script>

<template>
  <section id="main-content" class="chat-canvas liquid-glass-strong" aria-label="对话工作区">
    <p class="sr-only" role="status" aria-live="polite" aria-atomic="true">{{ announcement }}</p>
    <div ref="chatStream" class="chat-stream" role="log" aria-live="polite" aria-relevant="additions">
      <section v-if="!messages.length && !hasAgent" class="onboarding-empty">
        <span class="empty-orbit"><Sparkles :size="26" /></span>
        <span class="section-kicker">开始使用 ContentAI</span>
        <h1>先创建一个内容账号</h1>
        <p>账号配置决定热点来源、选题判断标准和内容表达方式。创建后会自动进入属于它的新会话。</p>
        <div class="template-actions">
          <button type="button" class="template-card" @click="emit('createAgent', 'finance')">
            <strong>财经解读</strong><span>关注商业、消费与政策变化</span>
          </button>
          <button type="button" class="template-card" @click="emit('createAgent', 'ai')">
            <strong>AI 资讯</strong><span>跟踪产品、技术与行业应用</span>
          </button>
        </div>
      </section>

      <section v-else-if="!messages.length" class="onboarding-empty compact">
        <span class="empty-orbit"><MessageSquareText :size="24" /></span>
        <span class="section-kicker">新会话</span>
        <h1>今天想做什么内容？</h1>
        <p>热点、选题判断、研究和最终稿都会在这段对话里完成。</p>
        <div class="suggestion-row">
          <button type="button" @click="useSuggestion('获取今日热点，并按我的账号定位给出推荐顺序')">获取今日热点</button>
          <button type="button" @click="useSuggestion('研究这个选题，列出可靠来源、关键事实和主要风险')">研究一个选题</button>
        </div>
      </section>

      <article
        v-for="(message, index) in messages"
        :key="index"
        class="message"
        :class="[message.role, { 'is-collapsed': isLong(message) && !expanded.has(index) }]"
      >
        <header class="message-header">
          <span class="message-role">{{ message.role === 'user' ? '你' : 'ContentAI' }}</span>
          <button
            v-if="message.role === 'assistant' && message.content"
            class="message-copy"
            type="button"
            :aria-label="copiedIndex === index ? '已复制' : '复制回复'"
            @click="copyMessage(message, index)"
          >
            <Check v-if="copiedIndex === index" :size="14" />
            <Copy v-else :size="14" />
            <span>{{ copiedIndex === index ? '已复制' : '复制' }}</span>
          </button>
        </header>
        <div class="message-bubble">
          <div
            v-if="message.role === 'assistant' && message.assistant_state && message.assistant_state !== 'normal'"
            class="assistant-progress"
            aria-hidden="true"
          >
            <LoaderCircle :size="14" class="spin" />
            <span>{{ message.assistant_state === 'pending' ? '等待响应' : '正在生成' }}</span>
          </div>
          <div v-if="message.content" class="message-renderer" v-html="renderMessage(message)"></div>
          <span v-else-if="message.role === 'assistant'" class="typing-placeholder" aria-hidden="true"><i></i><i></i><i></i></span>
        </div>
        <button
          v-if="isLong(message)"
          class="message-expand"
          type="button"
          :aria-expanded="expanded.has(index)"
          @click="toggleExpanded(index)"
        >
          {{ expanded.has(index) ? '收起回复' : '展开完整回复' }}
        </button>
      </article>
    </div>

    <div v-if="canResume" class="resume-card liquid-glass" role="region" aria-label="等待确认">
      <div><strong>需要你的确认</strong><span>{{ interruptSummary }}</span></div>
      <div class="resume-controls">
        <input v-model="resumePrompt" type="text" autocomplete="off" placeholder="输入确认或补充信息" @keydown.enter.prevent="resume" />
        <button type="button" :disabled="!resumePrompt.trim() || resumeSubmitting" @click="resume">
          <LoaderCircle v-if="resumeSubmitting" :size="16" class="spin" /><Play v-else :size="16" />
          {{ resumeSubmitting ? '提交中' : '继续' }}
        </button>
      </div>
    </div>

    <footer class="composer-wrap">
      <p v-if="promptError" class="field-error" role="alert">{{ promptError }}</p>
      <div class="composer liquid-glass">
        <textarea
          ref="promptInput"
          v-model="prompt"
          rows="2"
          aria-label="对话输入"
          placeholder="描述你想讨论、研究或创作的内容"
          :disabled="switchingAgent"
          @input="promptError = ''"
          @keydown.enter.prevent.exact="send"
          @keydown.ctrl.enter.prevent="send"
          @keydown.meta.enter.prevent="send"
        />
        <button v-if="isActive" class="send-button stop" type="button" :disabled="lifecycle === 'cancelling'" @click="cancelRun">
          <LoaderCircle v-if="lifecycle === 'cancelling'" :size="17" class="spin" />
          <Square v-else :size="15" fill="currentColor" />
          <span>{{ lifecycle === 'cancelling' ? '停止中' : '停止' }}</span>
        </button>
        <button v-else class="send-button" type="button" :disabled="sendDisabled" @click="send">
          <Play :size="17" />
          <span>发送</span>
        </button>
      </div>
      <span class="composer-hint">Enter 发送 · Shift + Enter 换行</span>
    </footer>
  </section>
</template>
