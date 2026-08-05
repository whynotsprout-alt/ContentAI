<script setup lang="ts">
import { computed, ref } from 'vue';
import { History, MessageCirclePlus, Search, Trash2 } from '@lucide/vue';
import type { ChatSessionSummary } from '@/shared/services/api';

const props = withDefaults(defineProps<{
  sessions: ChatSessionSummary[];
  activeSessionId: string;
  busy?: boolean;
  canCreate?: boolean;
}>(), {
  busy: false,
  canCreate: true
});

const emit = defineEmits<{
  create: [];
  select: [sessionId: string];
  delete: [session: ChatSessionSummary];
}>();

const search = ref('');

const visibleSessions = computed(() => {
  const term = search.value.trim().toLocaleLowerCase('zh-CN');
  return [...props.sessions]
    .filter((session) => !term || displayTitle(session).toLocaleLowerCase('zh-CN').includes(term))
    .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime());
});

function displayTitle(session: ChatSessionSummary) {
  if (session.title && session.title !== 'New Session') return session.title;
  return session.message_count ? '未命名会话' : '新会话';
}

function relativeDate(value: string) {
  const timestamp = new Date(value).getTime();
  const delta = Date.now() - timestamp;
  if (delta < 60_000) return '刚刚';
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(timestamp);
}

</script>

<template>
  <aside class="session-rail liquid-glass" aria-label="会话记录">
    <header class="session-rail-header">
      <span class="section-kicker"><History :size="15" /> 会话</span>
      <div class="session-rail-actions">
        <span class="session-count">{{ sessions.length }}</span>
        <button
          class="icon-action"
          type="button"
          aria-label="新建会话"
          :disabled="busy || !canCreate"
          @click="emit('create')"
        >
          <MessageCirclePlus :size="17" />
        </button>
      </div>
    </header>

    <div class="session-tools">
      <label class="compact-search">
        <Search :size="15" aria-hidden="true" />
        <input v-model="search" type="search" autocomplete="off" placeholder="搜索会话" aria-label="搜索会话" />
      </label>
    </div>

    <div class="session-list" aria-live="polite">
      <article
        v-for="session in visibleSessions"
        :key="session.session_id"
        class="session-card"
        :class="{ active: session.session_id === activeSessionId }"
      >
        <button
          class="session-select"
          type="button"
          :aria-current="session.session_id === activeSessionId ? 'page' : undefined"
          :title="displayTitle(session)"
          @click="emit('select', session.session_id)"
        >
          <span class="session-title">{{ displayTitle(session) }}</span>
          <span class="session-meta">
            {{ session.message_count }} 条消息 · {{ relativeDate(session.updated_at) }}
          </span>
        </button>
        <button
          class="session-delete"
          type="button"
          :aria-label="`删除会话：${displayTitle(session)}`"
          @click="emit('delete', session)"
        >
          <Trash2 :size="14" />
        </button>
      </article>

      <div v-if="!visibleSessions.length" class="session-empty">
        <strong>{{ sessions.length ? '没有匹配的会话' : '还没有会话' }}</strong>
        <span>{{ sessions.length ? '尝试修改搜索词或筛选条件。' : '发送第一条消息后，会话会保存在这里。' }}</span>
      </div>
    </div>
  </aside>
</template>
