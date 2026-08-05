<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  Bot,
  Check,
  ChevronDown,
  KeyRound,
  LogOut,
  Settings,
  ShieldCheck,
  Sparkles
} from '@lucide/vue';
import type { AgentProfile } from '@/shared/services/api';

const props = defineProps<{
  agents: AgentProfile[];
  activeAgentId: string;
  userEmail: string;
  isAdmin: boolean;
  switching: boolean;
}>();

const emit = defineEmits<{
  chooseAgent: [agentId: string];
  openAgents: [];
  openAdmin: [];
  changePassword: [];
  logout: [];
}>();

const agentMenuOpen = ref(false);
const userMenuOpen = ref(false);
const highlighted = ref(0);
const agentButton = ref<HTMLButtonElement | null>(null);
const userButton = ref<HTMLButtonElement | null>(null);

const selectedAgent = computed(() => props.agents.find((agent) => agent.id === props.activeAgentId));

function toggleAgentMenu() {
  userMenuOpen.value = false;
  agentMenuOpen.value = !agentMenuOpen.value;
  highlighted.value = Math.max(0, props.agents.findIndex((agent) => agent.id === props.activeAgentId));
}

function closeMenus(restoreFocus = false) {
  const restoreAgent = restoreFocus && agentMenuOpen.value;
  const restoreUser = restoreFocus && userMenuOpen.value;
  agentMenuOpen.value = false;
  userMenuOpen.value = false;
  if (restoreAgent || restoreUser) {
    void nextTick(() => (restoreAgent ? agentButton.value : userButton.value)?.focus());
  }
}

function onDocumentPointerDown(event: PointerEvent) {
  const target = event.target;
  if (target instanceof Node && (target as Element).closest('.header-popover')) return;
  closeMenus();
}

function onDocumentKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && (agentMenuOpen.value || userMenuOpen.value)) {
    event.preventDefault();
    closeMenus(true);
  }
}

function choose(agentId: string) {
  agentMenuOpen.value = false;
  emit('chooseAgent', agentId);
  void nextTick(() => agentButton.value?.focus());
}

function onAgentKeydown(event: KeyboardEvent) {
  if (!agentMenuOpen.value && ['ArrowDown', 'ArrowUp'].includes(event.key)) toggleAgentMenu();
  if (!agentMenuOpen.value || !props.agents.length) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeMenus(true);
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const direction = event.key === 'ArrowDown' ? 1 : -1;
    highlighted.value = (highlighted.value + direction + props.agents.length) % props.agents.length;
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    const agent = props.agents[highlighted.value];
    if (agent) choose(agent.id);
  }
}

function onUserKeydown(event: KeyboardEvent) {
  if (!userMenuOpen.value) return;
  const items = Array.from(document.querySelectorAll<HTMLElement>('#user-menu [role="menuitem"]'));
  if (!items.length) return;
  const currentIndex = items.indexOf(document.activeElement as HTMLElement);
  let nextIndex = currentIndex;
  if (event.key === 'ArrowDown') nextIndex = (currentIndex + 1 + items.length) % items.length;
  else if (event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + items.length) % items.length;
  else if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = items.length - 1;
  else return;
  event.preventDefault();
  items[nextIndex]?.focus();
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerDown);
  document.addEventListener('keydown', onDocumentKeydown);
});

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown);
  document.removeEventListener('keydown', onDocumentKeydown);
});
</script>

<template>
  <header class="workbench-header liquid-glass">
    <a class="brand-lockup" href="#main-content" aria-label="ContentAI，跳到对话区">
      <span class="brand-symbol"><Sparkles :size="18" /></span>
      <span>ContentAI</span>
    </a>

    <nav class="workbench-nav" aria-label="工作台导航">
      <button class="nav-button" data-agent-manager-trigger type="button" @click="emit('openAgents')">
        <Settings :size="16" /><span>内容账号</span>
      </button>
      <button v-if="isAdmin" class="nav-button" type="button" @click="emit('openAdmin')">
        <ShieldCheck :size="16" /><span>管理后台</span>
      </button>
    </nav>

    <div class="header-controls">
      <div class="header-popover" @keydown="onAgentKeydown">
        <button
          ref="agentButton"
          class="agent-picker-button"
          data-agent-manager-trigger
          type="button"
          role="combobox"
          :aria-expanded="agentMenuOpen"
          aria-haspopup="listbox"
          aria-controls="agent-options-list"
          :aria-activedescendant="agentMenuOpen && agents[highlighted] ? `agent-option-${agents[highlighted].id}` : undefined"
          :disabled="switching || !agents.length"
          @click="toggleAgentMenu"
        >
          <span class="agent-picker-icon"><Bot :size="15" /></span>
          <span class="agent-picker-label">{{ selectedAgent?.name ?? (agents.length ? '选择内容账号' : '尚未创建账号') }}</span>
          <ChevronDown :size="15" />
        </button>
        <div v-if="agentMenuOpen" class="popover-menu agent-options">
          <div id="agent-options-list" role="listbox" aria-label="选择内容账号">
            <button
              v-for="(agent, index) in agents"
              :key="agent.id"
              :id="`agent-option-${agent.id}`"
              type="button"
              role="option"
              tabindex="-1"
              :aria-selected="agent.id === activeAgentId"
              :class="{ highlighted: highlighted === index }"
              @mouseenter="highlighted = index"
              @click="choose(agent.id)"
            >
              <span><strong>{{ agent.name }}</strong><small>{{ agent.description || '未填写账号定位' }}</small></span>
              <Check v-if="agent.id === activeAgentId" :size="15" />
            </button>
          </div>
          <button class="menu-secondary" data-agent-manager-trigger type="button" @click="agentMenuOpen = false; emit('openAgents')">
            <Settings :size="15" /> 管理内容账号
          </button>
        </div>
      </div>

      <div class="header-popover" @keydown="onUserKeydown" @keydown.esc="closeMenus(true)">
        <button ref="userButton" class="user-menu-button" type="button" aria-label="打开用户菜单" title="用户菜单" :aria-expanded="userMenuOpen" aria-haspopup="menu" aria-controls="user-menu" @click="userMenuOpen = !userMenuOpen; agentMenuOpen = false">
          {{ userEmail.slice(0, 1).toUpperCase() }}
        </button>
        <div v-if="userMenuOpen" id="user-menu" class="popover-menu user-menu" role="menu">
          <span class="user-email">{{ userEmail }}</span>
          <button type="button" role="menuitem" @click="userMenuOpen = false; emit('changePassword')"><KeyRound :size="15" /> 修改密码</button>
          <button type="button" role="menuitem" @click="userMenuOpen = false; emit('logout')"><LogOut :size="15" /> 退出登录</button>
        </div>
      </div>
    </div>
  </header>
</template>
