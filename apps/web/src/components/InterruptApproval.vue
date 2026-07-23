<script setup lang="ts">
import { Ban, Check, LoaderCircle, Square } from '@lucide/vue';
import type { PublicInterrupt, ResumeDecision } from '../services/api';

defineProps<{
  pendingInterrupt: PublicInterrupt;
  busy: boolean;
  cancelling: boolean;
}>();

const emit = defineEmits<{
  decide: [decision: ResumeDecision];
  cancel: [];
}>();
</script>

<template>
  <section class="interrupt-approval" role="region" aria-labelledby="interrupt-approval-title">
    <header>
      <div>
        <h2 id="interrupt-approval-title">需要你的确认</h2>
        <p>请检查本批次的全部操作，再决定是否继续。</p>
      </div>
      <span>{{ pendingInterrupt.actions.length }} 项操作</span>
    </header>

    <ol class="interrupt-actions">
      <li
        v-for="(action, index) in pendingInterrupt.actions"
        :key="`${action.tool_name}-${action.purpose}-${index}`"
      >
        <div class="interrupt-action-heading">
          <span>{{ index + 1 }}</span>
          <code>{{ action.tool_name }}</code>
        </div>
        <p>{{ action.purpose }}</p>
        <dl v-if="action.memory">
          <div>
            <dt>记忆类型</dt>
            <dd>{{ action.memory.type }}</dd>
          </div>
          <div>
            <dt>记忆内容</dt>
            <dd>{{ action.memory.content }}</dd>
          </div>
        </dl>
      </li>
    </ol>

    <div class="interrupt-controls">
      <button
        class="interrupt-cancel"
        type="button"
        :disabled="busy || cancelling"
        @click="emit('cancel')"
      >
        <LoaderCircle v-if="cancelling" :size="16" class="spin" />
        <Square v-else :size="15" fill="currentColor" />
        {{ cancelling ? '取消中' : '取消本次运行' }}
      </button>
      <span class="interrupt-decision-controls">
        <button type="button" :disabled="busy || cancelling" @click="emit('decide', 'reject')">
          <Ban :size="16" />拒绝全部
        </button>
        <button class="primary" type="button" :disabled="busy || cancelling" @click="emit('decide', 'approve')">
          <LoaderCircle v-if="busy" :size="16" class="spin" />
          <Check v-else :size="16" />
          {{ busy ? '提交中' : '批准全部并继续' }}
        </button>
      </span>
    </div>
  </section>
</template>
