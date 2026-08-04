<script setup lang="ts">
import { computed } from 'vue';
import { AlertCircle, CheckCircle2, LoaderCircle, Radio, Square } from '@lucide/vue';
import type { RunLifecycle, TimelineEvent } from '@/features/workbench/stores/workbench.store';

const props = defineProps<{
  lifecycle: RunLifecycle;
  notice: string;
  events: TimelineEvent[];
  error?: string;
}>();

const activeEvent = computed(() => [...props.events].reverse().find((event) =>
  ['tool_start', 'tool_progress', 'tool_end'].includes(event.event)
));

const toolName = computed(() => {
  const data = activeEvent.value?.data;
  return typeof data?.tool_name === 'string'
    ? data.tool_name
    : typeof data?.name === 'string'
      ? data.name
      : '';
});

const toolProgress = computed(() => {
  const progress = activeEvent.value?.data?.progress;
  return progress && typeof progress === 'object'
    ? progress as Record<string, unknown>
    : {};
});

const toolLabel = computed(() => {
  const progressLabel = toolProgress.value.label;
  if (typeof progressLabel === 'string' && progressLabel.trim()) return progressLabel;
  if (activeEvent.value?.event === 'tool_end') return '工具执行完成，正在组织回答';
  if (toolName.value === 'fetch_hotspots') return '正在采集并筛选热点';
  if (toolName.value === 'prepare_topic_research') return '正在整理研究资料';
  if (toolName.value === 'recall_memory') return '正在读取相关偏好';
  if (toolName.value === 'remember') return '正在整理会话记忆';
  if (toolName.value === 'current_datetime') return '正在确认时间信息';
  return '正在组织回答';
});

const label = computed(() => {
  if (props.notice) return props.notice;
  if (props.lifecycle === 'queued') return '任务已提交，正在等待执行';
  if (props.lifecycle === 'reconnecting') return '实时连接中断，正在恢复';
  if (props.lifecycle === 'running') return toolLabel.value;
  if (props.lifecycle === 'waiting_input') return '需要你的确认后继续';
  if (props.lifecycle === 'cancelling') return '正在停止生成';
  if (props.lifecycle === 'failed') return props.error || '本次执行未完成';
  if (props.lifecycle === 'cancelled') return '生成已停止';
  return '';
});

const visible = computed(() => Boolean(label.value) && props.lifecycle !== 'completed' && props.lifecycle !== 'idle');
</script>

<template>
  <div
    v-if="visible"
    class="run-activity liquid-glass"
    :class="`state-${lifecycle}`"
    role="status"
    aria-live="polite"
    aria-atomic="true"
  >
    <LoaderCircle v-if="['queued', 'running', 'reconnecting', 'cancelling'].includes(lifecycle)" :size="16" class="spin" />
    <AlertCircle v-else-if="lifecycle === 'failed'" :size="16" />
    <Square v-else-if="lifecycle === 'cancelled'" :size="14" />
    <CheckCircle2 v-else-if="lifecycle === 'waiting_input'" :size="16" />
    <Radio v-else :size="16" />
    <span>{{ label }}</span>
  </div>
</template>
