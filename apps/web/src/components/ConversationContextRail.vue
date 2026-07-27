<script setup lang="ts">
import { computed } from 'vue';
import { Bot, Clock3, MessageSquareText, Plus, Settings2, Workflow } from '@lucide/vue';
import type { AgentProfile, ChatSessionDetail } from '../services/api';
import type { RunLifecycle } from '../stores/workbench';

const props = defineProps<{
  agent?: AgentProfile;
  session: ChatSessionDetail | null;
  lifecycle: RunLifecycle;
  messageCount: number;
  busy: boolean;
}>();

const emit = defineEmits<{
  createSession: [];
  manageAgent: [];
}>();

const statusMeta = computed(() => {
  const states: Record<RunLifecycle, { label: string; tone: string }> = {
    idle: { label: '等待指令', tone: 'quiet' },
    queued: { label: '正在排队', tone: 'working' },
    running: { label: '正在执行', tone: 'working' },
    reconnecting: { label: '正在重连', tone: 'working' },
    cancelling: { label: '正在停止', tone: 'working' },
    completed: { label: '执行完成', tone: 'success' },
    failed: { label: '执行失败', tone: 'danger' },
    cancelled: { label: '已停止', tone: 'quiet' },
    waiting_input: { label: '等待确认', tone: 'warning' }
  };
  return states[props.lifecycle];
});

const updatedAt = computed(() => {
  if (!props.session?.updated_at) return '尚未开始';
  const value = new Date(props.session.updated_at);
  if (Number.isNaN(value.getTime())) return '刚刚更新';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  }).format(value);
});

const sourceCount = computed(() => props.agent?.current_version?.hotspot_sources.length ?? 0);
</script>

<template>
  <aside class="conversation-context" aria-label="当前工作区信息">
    <section class="context-agent">
      <span class="context-kicker"><Workflow :size="14" /> 当前工作区</span>
      <span class="context-agent-icon"><Bot :size="20" /></span>
      <h2>{{ agent?.name ?? '尚未选择内容账号' }}</h2>
      <p>{{ agent?.description || '选择内容账号后，这里会显示它的定位与会话状态。' }}</p>
      <div v-if="agent?.current_version" class="context-agent-meta">
        <span>版本 {{ agent.current_version.version }}</span>
        <span>{{ sourceCount }} 个热点源</span>
      </div>
    </section>

    <section class="context-session" aria-labelledby="context-session-title">
      <header>
        <span class="context-section-icon"><MessageSquareText :size="16" /></span>
        <div>
          <span>本次会话</span>
          <h3 id="context-session-title">{{ session?.title || '未命名会话' }}</h3>
        </div>
      </header>

      <dl class="context-facts">
        <div>
          <dt>运行状态</dt>
          <dd :class="`context-status status-${statusMeta.tone}`"><i></i>{{ statusMeta.label }}</dd>
        </div>
        <div>
          <dt>消息数量</dt>
          <dd>{{ messageCount }} 条</dd>
        </div>
        <div>
          <dt>最近更新</dt>
          <dd><Clock3 :size="13" />{{ updatedAt }}</dd>
        </div>
      </dl>
    </section>

    <div class="context-actions">
      <button type="button" class="context-primary-action" :disabled="busy || !agent" @click="emit('createSession')">
        <Plus :size="16" /> 新建会话
      </button>
      <button type="button" class="context-secondary-action" data-agent-manager-trigger @click="emit('manageAgent')">
        <Settings2 :size="16" /> 管理内容账号
      </button>
    </div>
  </aside>
</template>
