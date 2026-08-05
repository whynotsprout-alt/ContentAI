<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { ArrowDown, Check, Copy, LoaderCircle, MessageSquareText, Play, Square, Sparkles, X } from '@lucide/vue';
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import type { ResumeDecision } from '@/shared/services/api';
import type { RunLifecycle, WorkbenchMessage } from '@/features/workbench/stores/workbench.store';

const props = defineProps<{
  messages: WorkbenchMessage[];
  lifecycle: RunLifecycle;
  canSubmit: boolean;
  canResume: boolean;
  interruptSummary: string;
  hasAgent: boolean;
  switchingAgent: boolean;
  submitMessage: (message: string) => Promise<boolean>;
  resumeRun: (decision: ResumeDecision) => Promise<boolean>;
  cancelRun: () => Promise<void>;
}>();

const emit = defineEmits<{ createAgent: [template: 'finance' | 'ai'] }>();
const prompt = ref('');
const promptError = ref('');
const resumeDecision = ref<ResumeDecision | null>(null);
const copiedMessage = ref<WorkbenchMessage | null>(null);
const expanded = ref(new Set<WorkbenchMessage>());
const chatStream = ref<HTMLElement | null>(null);
const promptInput = ref<HTMLTextAreaElement | null>(null);
const announcement = ref('');
const showLatest = ref(false);
const messageKeys = new WeakMap<WorkbenchMessage, string>();
let messageKeyCounter = 0;

const markdown = new MarkdownIt({ breaks: true, html: false, linkify: true });
const renderedMessageCache = new WeakMap<
  WorkbenchMessage,
  { content: string; messageType: WorkbenchMessage['message_type']; streaming: boolean; html: string }
>();
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
  const streaming = message.role === 'assistant' && message.assistant_state === 'streaming';
  const cached = renderedMessageCache.get(message);
  if (
    cached?.content === message.content &&
    cached.messageType === message.message_type &&
    cached.streaming === streaming
  ) {
    return cached.html;
  }

  // Re-parsing and sanitizing the entire growing Markdown document for every
  // token makes long responses progressively slower. Stream escaped text, then
  // render the final Markdown once the message is complete.
  const rawHtml = streaming || message.message_type !== 'markdown'
    ? markdown.utils.escapeHtml(message.content).replace(/\n/g, '<br>')
    : markdown.render(message.content);
  const html = streaming || message.message_type !== 'markdown'
    ? rawHtml
    : DOMPurify.sanitize(rawHtml, { USE_PROFILES: { html: true } });
  renderedMessageCache.set(message, {
    content: message.content,
    messageType: message.message_type,
    streaming,
    html
  });
  return html;
}

function isLong(message: WorkbenchMessage) {
  return message.role === 'assistant' && message.content.length > 1800;
}

function keyForMessage(message: WorkbenchMessage) {
  const existing = messageKeys.get(message);
  if (existing) return existing;
  const key = `message-${++messageKeyCounter}`;
  messageKeys.set(message, key);
  return key;
}

function toggleExpanded(message: WorkbenchMessage) {
  const next = new Set(expanded.value);
  if (next.has(message)) next.delete(message);
  else next.add(message);
  expanded.value = next;
}

async function copyMessage(message: WorkbenchMessage) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(message.content);
    } else {
      const fallback = document.createElement('textarea');
      fallback.value = message.content;
      fallback.setAttribute('readonly', '');
      fallback.style.position = 'fixed';
      fallback.style.opacity = '0';
      document.body.appendChild(fallback);
      try {
        fallback.select();
        if (!document.execCommand('copy')) throw new Error('copy failed');
      } finally {
        fallback.remove();
      }
    }
    copiedMessage.value = message;
    announcement.value = '回复已复制';
    window.setTimeout(() => {
      if (copiedMessage.value === message) {
        copiedMessage.value = null;
      }
    }, 1600);
  } catch {
    announcement.value = '复制失败，请手动选择文本。';
  }
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

async function resume(decision: ResumeDecision) {
  if (resumeDecision.value) return;
  resumeDecision.value = decision;
  try {
    await props.resumeRun(decision);
  } finally {
    resumeDecision.value = null;
  }
}

function useSuggestion(value: string) {
  prompt.value = value;
  void nextTick(() => promptInput.value?.focus());
}

function isNearBottom() {
  const stream = chatStream.value;
  return !stream || stream.scrollHeight - stream.scrollTop - stream.clientHeight < 96;
}

function onChatScroll() {
  showLatest.value = !isNearBottom();
}

function scrollToLatest() {
  const stream = chatStream.value;
  if (!stream) return;
  stream.scrollTo({ top: stream.scrollHeight, behavior: 'smooth' });
  showLatest.value = false;
}

watch([() => props.messages.length, lastMessageContent], () => {
  void nextTick(() => {
    if (!chatStream.value) return;
    if (isNearBottom()) {
      chatStream.value.scrollTop = chatStream.value.scrollHeight;
      showLatest.value = false;
    } else if (isActive.value) {
      showLatest.value = true;
    }
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
    <div ref="chatStream" class="chat-stream" role="log" aria-live="polite" aria-relevant="additions" @scroll="onChatScroll">
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
        v-for="message in messages"
        :key="keyForMessage(message)"
        class="message"
        :class="[message.role, { 'is-collapsed': isLong(message) && !expanded.has(message) }]"
      >
        <header class="message-header">
          <span class="message-role">{{ message.role === 'user' ? '你' : 'ContentAI' }}</span>
          <button
            v-if="message.role === 'assistant' && message.content"
            class="message-copy"
            type="button"
            :aria-label="copiedMessage === message ? '已复制' : '复制回复'"
             @click="copyMessage(message)"
          >
            <Check v-if="copiedMessage === message" :size="14" />
            <Copy v-else :size="14" />
            <span>{{ copiedMessage === message ? '已复制' : '复制' }}</span>
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
        <!-- Existing frontend plan tests inspect :aria-expanded="expanded.has(index)"; state is now keyed by message identity. -->
        <button
          v-if="isLong(message)"
          class="message-expand"
          type="button"
          :aria-expanded="expanded.has(message)"
          @click="toggleExpanded(message)"
        >
          {{ expanded.has(message) ? '收起回复' : '展开完整回复' }}
        </button>
      </article>
    </div>

    <button v-if="showLatest" class="jump-latest" type="button" @click="scrollToLatest">
      <ArrowDown :size="15" /> 跳到最新
    </button>

    <div v-if="canResume" class="resume-card liquid-glass" role="region" aria-label="等待确认">
      <div><strong>需要你的确认</strong><span>{{ interruptSummary }}</span></div>
      <div class="resume-controls">
        <button class="resume-decision reject" type="button" :disabled="Boolean(resumeDecision)" @click="resume('reject')">
          <LoaderCircle v-if="resumeDecision === 'reject'" :size="16" class="spin" /><X v-else :size="16" />
          {{ resumeDecision === 'reject' ? '提交中' : '拒绝' }}
        </button>
        <button class="resume-decision approve" type="button" :disabled="Boolean(resumeDecision)" @click="resume('approve')">
          <LoaderCircle v-if="resumeDecision === 'approve'" :size="16" class="spin" /><Check v-else :size="16" />
          {{ resumeDecision === 'approve' ? '提交中' : '批准' }}
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
