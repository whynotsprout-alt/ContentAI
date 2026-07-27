<script setup lang="ts">
import { computed, nextTick, ref } from 'vue';
import {
  Bot,
  Check,
  ChevronDown,
  KeyRound,
  LogOut,
  Menu,
  Settings,
  ShieldCheck,
  Sparkles
} from '@lucide/vue';
import type { AgentProfile } from '../services/api';

const props = defineProps<{
  agents: AgentProfile[];
  activeAgentId: string;
  userEmail: string;
  isAdmin: boolean;
  switching: boolean;
  showSessionNavigationToggle: boolean;
  sessionNavigationExpanded: boolean;
}>();

const emit = defineEmits<{
  toggleSessionNavigation: [];
  chooseAgent: [agentId: string];
  openAgents: [];
  openAdmin: [];
  changePassword: [];
  logout: [];
}>();

const agentMenuOpen = ref(false);
const userMenuOpen = ref(false);
const agentButton = ref<HTMLButtonElement | null>(null);
const userButton = ref<HTMLButtonElement | null>(null);

const selectedAgent = computed(() => props.agents.find((agent) => agent.id === props.activeAgentId));

function toggleAgentMenu() {
  userMenuOpen.value = false;
  agentMenuOpen.value = !agentMenuOpen.value;
}

function choose(agentId: string) {
  agentMenuOpen.value = false;
  emit('chooseAgent', agentId);
  void nextTick(() => agentButton.value?.focus());
}

function closeAgentMenu() {
  if (!agentMenuOpen.value) return;
  agentMenuOpen.value = false;
  void nextTick(() => agentButton.value?.focus());
}

function closeUserMenu() {
  if (!userMenuOpen.value) return;
  userMenuOpen.value = false;
  void nextTick(() => userButton.value?.focus());
}
</script>

<template>
  <header class="workbench-header">
    <button
      v-if="showSessionNavigationToggle"
      class="session-nav-toggle"
      type="button"
      aria-controls="session-navigation"
      :aria-expanded="sessionNavigationExpanded"
      :aria-label="sessionNavigationExpanded ? '收起会话导航' : '打开会话导航'"
      @click="emit('toggleSessionNavigation')"
    >
      <Menu :size="20" />
    </button>

    <a class="brand-lockup" href="#main-content" aria-label="ContentAI，跳到对话区">
      <span class="brand-symbol"><Sparkles :size="18" /></span>
      <span>ContentAI</span>
    </a>

    <div class="header-controls">
      <div class="header-popover" @keydown.esc.prevent.stop="closeAgentMenu">
        <button
          ref="agentButton"
          class="agent-picker-button"
          data-agent-manager-trigger
          type="button"
          :aria-expanded="agentMenuOpen"
          aria-controls="agent-picker-options"
          :disabled="switching || !agents.length"
          @click="toggleAgentMenu"
        >
          <span class="agent-picker-icon"><Bot :size="15" /></span>
          <span class="agent-picker-label">{{ selectedAgent?.name ?? (agents.length ? '选择内容账号' : '尚未创建账号') }}</span>
          <ChevronDown :size="15" />
        </button>
        <div v-if="agentMenuOpen" id="agent-picker-options" class="popover-menu agent-options" aria-label="选择内容账号">
          <button
            v-for="agent in agents"
            :key="agent.id"
            type="button"
            @click="choose(agent.id)"
          >
            <span><strong>{{ agent.name }}</strong><small>{{ agent.description || '未填写账号定位' }}</small></span>
            <Check v-if="agent.id === activeAgentId" :size="15" />
          </button>
          <button class="menu-secondary" data-agent-manager-trigger type="button" @click="agentMenuOpen = false; emit('openAgents')">
            <Settings :size="15" /> 管理内容账号
          </button>
        </div>
      </div>

      <button v-if="isAdmin" class="nav-button header-admin-button" type="button" aria-label="管理后台" @click="emit('openAdmin')">
        <ShieldCheck :size="16" /><span>管理后台</span>
      </button>

      <div class="header-popover" @keydown.esc.prevent.stop="closeUserMenu">
        <button
          ref="userButton"
          class="user-menu-button"
          type="button"
          :aria-label="`账号菜单：${userEmail}`"
          :aria-expanded="userMenuOpen"
          aria-controls="user-account-menu"
          @click="userMenuOpen = !userMenuOpen; agentMenuOpen = false"
        >
          {{ userEmail.slice(0, 1).toUpperCase() }}
        </button>
        <div v-if="userMenuOpen" id="user-account-menu" class="popover-menu user-menu">
          <span class="user-email">{{ userEmail }}</span>
          <button class="user-menu-mobile-action" data-agent-manager-trigger type="button" @click="userMenuOpen = false; emit('openAgents')"><Settings :size="15" /> 内容账号</button>
          <button v-if="isAdmin" class="user-menu-mobile-action" type="button" @click="userMenuOpen = false; emit('openAdmin')"><ShieldCheck :size="15" /> 管理后台</button>
          <button type="button" @click="userMenuOpen = false; emit('changePassword')"><KeyRound :size="15" /> 修改密码</button>
          <button type="button" @click="userMenuOpen = false; emit('logout')"><LogOut :size="15" /> 退出登录</button>
        </div>
      </div>
    </div>
  </header>
</template>
