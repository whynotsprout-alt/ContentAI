<script setup lang="ts">
import { computed, ref } from 'vue';
import { History, MessageCircle, MessageCirclePlus, Search, Trash2, X } from '@lucide/vue';
import type { ChatSessionSummary } from '../services/api';

const props = withDefaults(defineProps<{
  sessions: ChatSessionSummary[];
  activeSessionId: string;
  busy?: boolean;
  canCreate?: boolean;
  hasMore?: boolean;
  loadingMore?: boolean;
  compact?: boolean;
  drawer?: boolean;
}>(), {
  busy: false,
  canCreate: true,
  hasMore: false,
  loadingMore: false,
  compact: false,
  drawer: false
});

const emit = defineEmits<{
  close: [];
  create: [];
  loadMore: [];
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
  <aside
    class="session-rail"
    aria-label="会话记录"
    :role="drawer ? 'dialog' : 'navigation'"
    :aria-modal="drawer ? 'true' : undefined"
  >
    <header class="session-rail-header">
      <h2><History :size="18" /><span v-if="!compact">会话</span></h2>
      <div class="session-rail-actions">
        <span v-if="!compact" class="session-count">{{ sessions.length }}</span>
        <button
          class="icon-action"
          type="button"
          aria-label="新建会话"
          :disabled="busy || !canCreate"
          @click="emit('create')"
        >
          <MessageCirclePlus :size="17" />
        </button>
        <button
          v-if="drawer"
          class="icon-action"
          data-session-drawer-close
          type="button"
          aria-label="关闭会话导航"
          @click="emit('close')"
        >
          <X :size="18" />
        </button>
      </div>
    </header>

    <div v-if="!compact" class="session-tools">
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
          :aria-label="compact ? displayTitle(session) : undefined"
          @click="emit('select', session.session_id)"
        >
          <MessageCircle v-if="compact" :size="18" aria-hidden="true" />
          <span v-if="!compact" class="session-title">{{ displayTitle(session) }}</span>
          <span v-if="!compact" class="session-meta">
            {{ session.message_count }} 条消息 · {{ relativeDate(session.updated_at) }}
          </span>
        </button>
        <button
          v-if="!compact"
          class="session-delete"
          type="button"
          :aria-label="`删除会话：${displayTitle(session)}`"
          @click="emit('delete', session)"
        >
          <Trash2 :size="14" />
        </button>
      </article>

      <div v-if="!visibleSessions.length && !compact" class="session-empty">
        <strong>{{ sessions.length ? '没有匹配的会话' : '还没有会话' }}</strong>
        <span>{{ sessions.length ? '尝试修改搜索词或筛选条件。' : '发送第一条消息后，会话会保存在这里。' }}</span>
      </div>
      <button v-if="hasMore" type="button" class="session-load-more" :disabled="busy || loadingMore" @click="emit('loadMore')">
        {{ loadingMore ? '正在加载会话…' : '加载更多会话' }}
      </button>
    </div>
  </aside>
</template>
