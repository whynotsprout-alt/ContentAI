import { defineStore } from 'pinia';
import {
  ApiError,
  api,
  type AgentProfile,
  type AgentProfilePayload,
  type AgentVersionPayload,
  type ChatSessionSummary,
  type AgentProfileUpdatePayload,
  type ChatSessionDetail,
  type ChatExecutionInfo,
  type FetchEventStream,
  type PublicInterrupt,
  type ResumeDecision
} from '../services/api';

const SSE_RECOVERY_INITIAL_DELAY_MS = 1000;
const SSE_RECOVERY_MAX_DELAY_MS = 10000;
const SSE_RECOVERY_MAX_ATTEMPTS = 8;
const RUN_STATUS_POLL_MS = 3000;
const STREAM_DEGRADED_CODES = new Set([
  'STREAMING_DEGRADED',
  'STREAM_REPLAY_GAP',
  'STREAM_REPLAY_EXPIRED',
  'REDIS_READ_FAILED'
]);

export type AssistantBubbleState = 'normal' | 'pending' | 'streaming';
export type RunLifecycle = 'idle' | 'queued' | 'running' | 'reconnecting' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'waiting_input';

export interface TimelineEvent {
  event: string;
  data: Record<string, unknown>;
}

export type WorkbenchMessage = {
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  message_type: 'text' | 'markdown' | 'json';
  assistant_state?: AssistantBubbleState;
};

function normalizeErrorMessage(value: unknown) {
  if (value instanceof ApiError) {
    if (value.status === 401) return '登录状态已失效，请重新登录。';
    if (value.status === 403 && /disabled|禁用/i.test(value.message)) return '账号已被禁用，无法继续操作。';
    if (value.status === 429) {
      return /daily|budget|日预算|额度/i.test(`${value.code} ${value.message}`)
        ? '今日模型额度已用完，请明天再试或联系管理员调整预算。'
        : '请求过于频繁，请稍后再试。';
    }
    const suffix = value.requestId ? `（请求标识：${value.requestId}）` : '';
    return `${value.message || '请求失败'}${suffix}`;
  }
  const message = value instanceof Error ? value.message : String(value || '');
  const parsed = parseApiErrorDetail(message);
  const normalized = parsed.trim().replace(/\s+/g, ' ');
  if (!normalized) return '执行失败';
  const lowered = normalized.toLowerCase();
  if (lowered.includes('<html') || lowered.includes('<!doctype html')) {
    if (lowered.includes('service suspended')) {
      return '模型服务暂不可用：当前配置的模型网关服务已暂停，请检查模型服务配置。';
    }
    return '模型服务返回了异常页面，请检查模型服务配置。';
  }
  if (lowered.includes('service suspended')) {
    return '模型服务暂不可用：当前配置的模型网关服务已暂停，请检查模型服务配置。';
  }
  if (lowered.includes('401') || lowered.includes('unauthorized') || lowered.includes('invalid api key')) {
    return '模型服务认证失败：请检查 API Key 是否有效。';
  }
  if (lowered.includes('403') || lowered.includes('forbidden')) {
    return '模型服务拒绝访问：请检查模型权限或 API Key 权限。';
  }
  if (lowered.includes('quota') || lowered.includes('billing') || lowered.includes('insufficient')) {
    return '模型服务额度不足或计费异常：请检查模型服务账户额度。';
  }
  return normalized.slice(0, 800);
}

function formatStreamErrorMessage(message: unknown, data: Record<string, unknown>) {
  const baseMessage = normalizeErrorMessage(message);
  const code = typeof data.code === 'string' && data.code.trim()
    ? data.code.trim()
    : typeof data.error_code === 'string' && data.error_code.trim()
      ? data.error_code.trim()
      : '';
  const requestId = typeof data.request_id === 'string' ? data.request_id.trim() : '';
  const sessionId = typeof data.session_id === 'string' ? data.session_id.trim() : '';
  const threadId = typeof data.thread_id === 'string' ? data.thread_id.trim() : '';
  const executionId = typeof data.execution_id === 'string' ? data.execution_id.trim() : '';
  const extras = [
    code ? `错误码: ${code}` : '',
    requestId ? `request_id: ${requestId}` : '',
    sessionId ? `session_id: ${sessionId}` : '',
    threadId ? `thread_id: ${threadId}` : '',
    executionId ? `execution_id: ${executionId}` : ''
  ].filter(Boolean);
  return extras.length ? `${baseMessage}（${extras.join('，')}）` : baseMessage;
}

function parseApiErrorDetail(message: string) {
  try {
    const parsed = JSON.parse(message) as { detail?: unknown; error?: unknown; message?: unknown };
    const detail = parsed.detail ?? parsed.error ?? parsed.message;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map((item) => item?.msg || JSON.stringify(item)).join('；');
    if (detail) return JSON.stringify(detail);
  } catch {
    // keep original message
  }
  return message;
}

function toRunLifecycle(status: string | null | undefined): RunLifecycle {
  const normalized = typeof status === 'string' ? status.toLowerCase() : 'idle';
  if (normalized === 'pending') return 'queued';
  if (normalized === 'running') return 'running';
  if (normalized === 'completed') return 'completed';
  if (normalized === 'failed') return 'failed';
  if (normalized === 'cancelled' || normalized === 'canceled') return 'cancelled';
  if (normalized === 'waiting_input') return 'waiting_input';
  return 'idle';
}

function queueStageNotice(execution: ChatExecutionInfo | null | undefined) {
  if (execution?.queue_stage === 'dispatching') return '任务已入队，正在提交到执行队列…';
  if (execution?.queue_stage === 'waiting_worker') return '任务已入队，正在等待 Worker…';
  if (execution?.queue_stage === 'starting') return 'Worker 已领取任务，正在启动…';
  return '';
}

function resolveSessionStatus(session: ChatSessionDetail, fallback: string = 'idle') {
  return typeof session.latest_execution?.status === 'string' && session.latest_execution.status
    ? session.latest_execution.status
    : session.latest_execution_status ?? fallback;
}

function streamEventSequence(event: MessageEvent) {
  const rawId = String(event.lastEventId || '').trim();
  const idMatch = rawId.match(/(?:^|:)(\d+)$/);
  if (idMatch?.[1]) return Number(idMatch[1]);
  try {
    const envelope = JSON.parse(String(event.data || '')) as { sequence?: unknown };
    const sequence = Number(envelope.sequence);
    return Number.isInteger(sequence) && sequence > 0 ? sequence : 0;
  } catch {
    return 0;
  }
}

function publicInterrupt(value: unknown): PublicInterrupt | null {
  if (!value || typeof value !== 'object') return null;
  const interrupt = value as Record<string, unknown>;
  const interruptId = typeof interrupt.interrupt_id === 'string' ? interrupt.interrupt_id.trim() : '';
  if (!interruptId || !Array.isArray(interrupt.actions) || !interrupt.actions.length) return null;
  const actions = interrupt.actions.map((value) => {
    if (!value || typeof value !== 'object') return null;
    const action = value as Record<string, unknown>;
    const toolName = typeof action.tool_name === 'string' ? action.tool_name.trim() : '';
    const purpose = typeof action.purpose === 'string' ? action.purpose.trim() : '';
    if (!toolName || !purpose) return null;
    const memoryValue = action.memory;
    if (memoryValue === null || memoryValue === undefined) return { tool_name: toolName, purpose, memory: null };
    if (typeof memoryValue !== 'object') return null;
    const memory = memoryValue as Record<string, unknown>;
    const type = typeof memory.type === 'string' ? memory.type.trim() : '';
    const content = typeof memory.content === 'string' ? memory.content.trim() : '';
    return type && content ? { tool_name: toolName, purpose, memory: { type, content } } : null;
  });
  return actions.every((action): action is NonNullable<typeof action> => action !== null)
    ? { interrupt_id: interruptId, actions }
    : null;
}

export const useWorkbenchStore = defineStore('workbench', {
  state: () => ({
    agents: [] as AgentProfile[],
    sessions: [] as ChatSessionSummary[],
    sessionNextCursor: null as string | null,
    isLoadingSessions: false,
    sessionRequestSequence: 0,
    sessionRequestOwner: null as { id: number; agentId: string; cursor: string; contextToken: number } | null,
    messageNextCursor: null as string | null,
    agentId: '',
    sessionId: '',
    executionId: '',
    activeSessionId: '',
    status: 'idle',
    messages: [] as WorkbenchMessage[],
    events: [] as TimelineEvent[],
    sessionInfo: null as ChatSessionDetail | null,
    executionInfo: null as ChatExecutionInfo | null,
    pendingInterrupt: null as PublicInterrupt | null,
    error: '',
    assistantStreamingBuffer: '',
    assistantStreamingState: 'normal' as AssistantBubbleState,
    isStreamingAssistantMessage: false,
    streamingAssistantMessageIndex: -1,
    runLifecycle: 'idle' as RunLifecycle,
    statusNotice: '',
    ssePollTimer: null as number | null,
    eventSource: null as FetchEventStream | null,
    sseRecoveryAttempts: 0,
    processedSseEventIds: [] as string[],
    lastEventSequence: 0,
    sessionContextToken: 0,
    isSwitchingAgent: false,
    isLoadingSession: false,
    lastErrorCode: ''
  }),
  getters: {
    canSubmit(state) {
      return Boolean(
        state.sessionId &&
          state.agentId &&
          (!state.sessionInfo || state.sessionInfo.agent_id === state.agentId) &&
          !state.isSwitchingAgent &&
          !state.isLoadingSession &&
          !['queued', 'running', 'reconnecting', 'cancelling', 'waiting_input'].includes(state.runLifecycle)
      );
    },
    canResume(state) {
      return Boolean(
        state.sessionId &&
          state.pendingInterrupt &&
          (state.status === 'waiting_input' || state.runLifecycle === 'waiting_input')
      );
    },
    selectedAgent(state) {
      return state.agents.find((item) => item.id === state.agentId);
    }
  },
  actions: {
    async boot() {
      await this.refreshAgents();
      if (!this.agentId) {
        this.sessions = [];
        this.sessionNextCursor = null;
        this.resetConversationContext();
        return;
      }
      await this.refreshSessions();
      if (this.sessions.length) {
        await this.loadSession(this.sessions[0].session_id);
      } else {
        await this.startNewSession();
      }
    },

    async refreshSessions(cursor = '', append = false) {
      const agentId = this.agentId;
      const contextToken = this.sessionContextToken;
      const current = this.sessionRequestOwner;
      if (current && current.agentId === agentId && current.cursor === cursor && current.contextToken === contextToken) return;
      const owner = {
        id: this.sessionRequestSequence + 1,
        agentId,
        cursor,
        contextToken
      };
      this.sessionRequestSequence = owner.id;
      this.sessionRequestOwner = owner;
      this.isLoadingSessions = true;
      try {
        const result = await api.sessions(agentId, cursor);
        if (
          this.sessionRequestOwner?.id !== owner.id ||
          this.agentId !== owner.agentId ||
          this.sessionContextToken !== owner.contextToken
        ) return;
        this.sessions = append
          ? [...this.sessions, ...result.items.filter((session) => !this.sessions.some((item) => item.session_id === session.session_id))]
          : result.items;
        this.sessionNextCursor = result.next_cursor;
      } finally {
        if (this.sessionRequestOwner?.id === owner.id) {
          this.sessionRequestOwner = null;
          this.isLoadingSessions = false;
        }
      }
    },

    async loadMoreSessions() {
      if (!this.sessionNextCursor || !this.agentId) return;
      await this.refreshSessions(this.sessionNextCursor, true);
    },

    async chooseAgent(agentId: string, force = false) {
      if (!force && this.agentId === agentId && this.sessionInfo?.agent_id === agentId) return true;
      const contextToken = this._beginConversationContext();
      this.agentId = agentId;
      this.sessions = [];
      this.sessionNextCursor = null;
      this.isSwitchingAgent = true;
      this.lastErrorCode = '';
      try {
        if (!this._isConversationContextCurrent(contextToken)) return false;
        await this.refreshSessions();
        if (!this._isConversationContextCurrent(contextToken)) return false;
        if (!this.sessions.length) {
          await this.startNewSession();
          return Boolean(this.sessionId && this.sessionInfo?.agent_id === agentId);
        }
        const targetSession = this.sessions[0];
        if (!targetSession) return false;
        await this.loadSession(targetSession.session_id);
        return Boolean(this.sessionId && this.sessionInfo?.agent_id === agentId);
      } catch (error) {
        if (this._isConversationContextCurrent(contextToken)) {
          this.lastErrorCode = error instanceof ApiError ? error.code : '';
          this.error = normalizeErrorMessage(error);
        }
        return false;
      } finally {
        if (this.agentId === agentId) this.isSwitchingAgent = false;
      }
    },

    async startNewSession() {
      const contextToken = this._beginConversationContext();
      const requestedAgentId = this.agentId;
      this.eventSource?.close();
      this.eventSource = null;
      if (!this._isConversationContextCurrent(contextToken)) return;
      this.error = '';
      this.statusNotice = '';
      this.status = 'idle';
      this.runLifecycle = 'idle';
      this.messages = [];
      this.events = [];
      this.sessionInfo = null;
      this.executionInfo = null;
      this.pendingInterrupt = null;
      this.executionId = '';
      this.activeSessionId = '';

      if (!this.agentId) {
        return false;
      }
      this.isLoadingSession = true;
      try {
        const session = await api.createSession({ agent_id: this.agentId });
        if (!this._isConversationContextCurrent(contextToken)) return false;
        this.sessionId = '';
        await this.loadSession(session.session_id);
        await this.refreshSessions();
        return Boolean(this.sessionId && this.agentId === requestedAgentId);
      } catch (error) {
        if (this.agentId === requestedAgentId) {
          this.lastErrorCode = error instanceof ApiError ? error.code : '';
          this.error = normalizeErrorMessage(error);
        }
        return false;
      } finally {
        if (this.agentId === requestedAgentId) this.isLoadingSession = false;
      }
    },

    async loadSession(sessionId: string): Promise<boolean> {
      if (!sessionId || (sessionId === this.sessionId && this.sessionInfo)) return true;
      const contextToken = this._beginConversationContext();
      const requestedAgentId = this.agentId;
      this.isLoadingSession = true;
      this.eventSource?.close();
      this.eventSource = null;
      if (!this._isConversationContextCurrent(contextToken)) return false;
      this._stopSseRecoveryPoll();
      if (!this._isConversationContextCurrent(contextToken)) return false;
      this.processedSseEventIds = [];
      this.lastEventSequence = 0;
      this.sessionInfo = null;
      this.executionInfo = null;
      this.pendingInterrupt = null;

      try {
        const session = await api.session(sessionId);
        if (!this._isConversationContextCurrent(contextToken)) return false;
        if (this.agentId && session.agent_id !== this.agentId) {
          const selectedAgentId = this.agentId;
          this.resetConversationContext();
          this.lastErrorCode = 'SESSION_AGENT_MISMATCH';
          this.error = '当前会话与所选 Agent 不一致，正在恢复正确会话。';
          await this.refreshSessions();
          const replacement = this.sessions.find((item) => item.agent_id === selectedAgentId && item.session_id !== sessionId);
          if (replacement) return this.loadSession(replacement.session_id);
          return Boolean(await this.startNewSession());
        }
        this.sessionId = session.session_id;
        this.messages = session.messages
          .filter((message) => ['text', 'markdown'].includes(message.message_type))
          .map((message) => ({
            id: message.id,
            role: message.role as WorkbenchMessage['role'],
            content: message.content,
            message_type: message.message_type,
            assistant_state: 'normal'
          }));
        this.messageNextCursor = session.next_cursor;
        this.executionId = session.latest_execution?.id ?? '';
        this.activeSessionId = '';
        const resolvedStatus = resolveSessionStatus(session, 'idle');
        this.status = resolvedStatus;
        this.runLifecycle = toRunLifecycle(resolvedStatus);
        this.assistantStreamingState = 'normal';

        this.sessionInfo = session;
        this._setExecutionInfo(session.latest_execution);
        if (['queued', 'running', 'reconnecting'].includes(this.runLifecycle)) {
          this._scheduleSseRecovery(session.session_id, contextToken);
          this._ensureAssistantPlaceholder();
          return true;
        }
        if (['failed', 'cancelled', 'waiting_input'].includes(this.status)) {
          this._removeActiveStreamingAssistantPlaceholder();
        }
        return true;
      } catch (error) {
        if (this.agentId === requestedAgentId) {
          this.lastErrorCode = error instanceof ApiError ? error.code : '';
          this.error = normalizeErrorMessage(error);
        }
        return false;
      } finally {
        if (this.agentId === requestedAgentId) this.isLoadingSession = false;
      }
    },

    resetConversationContext() {
      this.sessionId = '';
      this.executionId = '';
      this.activeSessionId = '';
      this.messages = [];
      this.events = [];
      this.sessionInfo = null;
      this.executionInfo = null;
      this.pendingInterrupt = null;
      this.messageNextCursor = null;
      this.error = '';
      this.lastErrorCode = '';
      this.statusNotice = '';
      this.status = 'idle';
      this.runLifecycle = 'idle';
      this.assistantStreamingState = 'normal';
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
      this.processedSseEventIds = [];
      this._stopSseRecoveryPoll();
      this.eventSource?.close();
      this.eventSource = null;
      this._removeActiveStreamingAssistantPlaceholder();
    },

    _beginConversationContext() {
      this.sessionContextToken += 1;
      this.resetConversationContext();
      return this.sessionContextToken;
    },

    _isConversationContextCurrent(contextToken: number) {
      return this.sessionContextToken === contextToken;
    },

    _findActiveAssistantPlaceholderIndex() {
      for (let index = this.messages.length - 1; index >= 0; index--) {
        const message = this.messages[index];
        if (message.role === 'assistant' && message.assistant_state && message.assistant_state !== 'normal') {
          return index;
        }
      }
      return -1;
    },

    async deleteSession(sessionId: string) {
      if (!sessionId) return;
      const deletingCurrentSession = sessionId === this.sessionId;
      try {
        await api.deleteSession(sessionId);
        await this.refreshSessions();
        if (!deletingCurrentSession) return;

        const contextToken = this._beginConversationContext();
        this.eventSource?.close();
        this.eventSource = null;
        this._stopSseRecoveryPoll();
        if (!this._isConversationContextCurrent(contextToken)) return;
        this._removeActiveStreamingAssistantPlaceholder();
        this.processedSseEventIds = [];
        this.error = '';
        this.statusNotice = '';
        this.messages = [];
        this.events = [];
        this.sessionInfo = null;
        this.executionInfo = null;
        this.pendingInterrupt = null;
        this.executionId = '';
        this.activeSessionId = '';
        this.status = 'idle';
        this.runLifecycle = 'idle';
        this.sessionId = '';

        if (this.sessions.length) {
          await this.loadSession(this.sessions[0].session_id);
        } else {
          await this.startNewSession();
        }
      } catch (error) {
        if (error instanceof ApiError && error.code === 'SESSION_HAS_ACTIVE_EXECUTION') {
          this.lastErrorCode = error.code;
          const session = this.sessions.find((item) => item.session_id === sessionId);
          const lifecycle = deletingCurrentSession
            ? this.runLifecycle
            : toRunLifecycle(session?.latest_execution_status);
          this.error = lifecycle === 'waiting_input'
            ? '当前会话正在等待确认，请继续或取消当前运行后再删除。'
            : '当前会话正在运行，完成或取消后再删除。';
          return;
        }
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        this.error = normalizeErrorMessage(error);
      }
    },

    async refreshAgents(preferredAgentId?: string) {
      const agents = await api.agents();
      this.agents = agents;
      const currentStillExists = agents.some((agent) => agent.id === this.agentId);
      if (preferredAgentId && agents.some((agent) => agent.id === preferredAgentId)) {
        this.agentId = preferredAgentId;
      } else if (!currentStillExists) {
        this.agentId = agents[0]?.id ?? '';
      }
    },

    async createAgent(payload: AgentProfilePayload) {
      const agent = await api.createAgent(payload);
      await this.refreshAgents(agent.id);
      await this.chooseAgent(agent.id);
      return agent;
    },

    async updateAgent(agentId: string, payload: AgentProfileUpdatePayload) {
      const agent = await api.updateAgent(agentId, payload);
      await this.refreshAgents();
      await this.chooseAgent(agent.id);
      return agent;
    },

    async createAgentVersion(agentId: string, payload: AgentVersionPayload) {
      await api.createAgentVersion(agentId, payload);
      await this.refreshAgents(agentId);
      return api.agent(agentId);
    },

    async deleteAgent(agentId: string) {
      const deletingCurrent = this.agentId === agentId;
      await api.deleteAgent(agentId);
      await this.refreshAgents(deletingCurrent ? undefined : this.agentId);
      if (deletingCurrent) {
        const nextAgent = this.agents[0];
        if (nextAgent) await this.chooseAgent(nextAgent.id);
        else await this.startNewSession();
      } else {
        await this.refreshSessions();
      }
    },

    async submit(message: string) {
      if (!this.canSubmit) return false;
      if (!this.agentId) {
        this.error = '请先选择或创建一个 Agent 后再发送消息。';
        return false;
      }
      if (!this.sessionId) {
        this.error = '当前没有可用会话，请先新建会话。';
        return false;
      }
      const contextToken = this.sessionContextToken;
      const requestSessionId = this.sessionId;
      let sessionInfo = this.sessionInfo;
      if (!sessionInfo || sessionInfo.session_id !== requestSessionId) {
        try {
          sessionInfo = await api.session(requestSessionId);
        } catch (error) {
          if (!this._isConversationContextCurrent(contextToken)) return false;
          this.error = normalizeErrorMessage(error);
          return false;
        }
        if (!this._isConversationContextCurrent(contextToken)) return false;
        this.sessionInfo = sessionInfo;
      }
      if (sessionInfo.agent_id !== this.agentId) {
        this.error = '当前会话与所选 Agent 不一致，请重新选择 Agent 或新建会话。';
        return false;
      }
      const requestIdempotencyKey = this._nextIdempotencyKey();
      this.error = '';
      this.lastErrorCode = '';
      this.statusNotice = '';
      this.status = 'pending';
      this.runLifecycle = 'queued';
      this.executionId = '';
      this.processedSseEventIds = [];
      this.lastEventSequence = 0;
      const optimisticUserMessage: WorkbenchMessage = { role: 'user', content: message, message_type: 'text', assistant_state: 'normal' };
      this.messages.push(optimisticUserMessage);
      this._ensureAssistantPlaceholder();
      try {
        const submitted = await api.sendMessage(requestSessionId, {
          message,
          idempotency_key: requestIdempotencyKey
        });
        if (!this._isConversationContextCurrent(contextToken)) return false;
        this.executionId = submitted.execution_id;
        this.listen(requestSessionId, contextToken, api.executionEvents(submitted.execution_id));
        await this.refreshSessions().catch(() => undefined);
        return true;
      } catch (error) {
        if (!this._isConversationContextCurrent(contextToken)) return false;
        const optimisticIndex = this.messages.indexOf(optimisticUserMessage);
        if (optimisticIndex >= 0) this.messages.splice(optimisticIndex, 1);
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        this.error = error instanceof ApiError && error.code === 'MODEL_NOT_CONFIGURED'
          ? '模型服务尚未配置，请联系管理员'
          : normalizeErrorMessage(error);
        this.status = 'failed';
        this.runLifecycle = 'failed';
        this._removeActiveStreamingAssistantPlaceholder();
        if (error instanceof ApiError && error.code === 'SESSION_AGENT_MISMATCH') {
          await this.chooseAgent(this.agentId, true);
        }
        return false;
      }
    },

    async resume(decision: ResumeDecision) {
      if (!this.canResume) return false;
      const contextToken = this.sessionContextToken;
      const requestSessionId = this.sessionId;
      this.error = '';
      this.lastErrorCode = '';
      this.statusNotice = '';
      this.status = 'pending';
      this.runLifecycle = 'queued';
      this._ensureAssistantPlaceholder();
      this.processedSseEventIds = [];
      this.lastEventSequence = 0;
      try {
        const requestExecutionId = this.executionId;
        if (!requestExecutionId) throw new Error('No run waiting for input to resume');
        if (this.sessionInfo?.agent_id !== this.agentId) {
          throw new Error('当前会话与所选 Agent 不一致，请重新选择 Agent 后继续。');
        }
        const interrupt = this.pendingInterrupt;
        if (!interrupt) throw new Error('当前确认请求已失效，请刷新会话后重试。');
        const execution = await api.resumeRun(requestExecutionId, {
          interrupt_id: interrupt.interrupt_id,
          decision
        });
        if (!this._isConversationContextCurrent(contextToken)) return false;
        this.executionId = execution.id;
        this._setExecutionInfo(execution);
        this.listen(requestSessionId, contextToken, api.executionEvents(execution.id));
        await this.refreshSessions().catch(() => undefined);
        return true;
      } catch (error) {
        if (!this._isConversationContextCurrent(contextToken)) return false;
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        this.error = normalizeErrorMessage(error);
        this.status = 'failed';
        this.runLifecycle = 'failed';
        this._removeActiveStreamingAssistantPlaceholder();
        await this.refreshSession(false).catch(() => undefined);
        if (error instanceof ApiError && error.code === 'SESSION_AGENT_MISMATCH') {
          await this.chooseAgent(this.agentId, true);
        }
        return false;
      }
    },

    async cancelActiveRun() {
      if (!['queued', 'running', 'reconnecting', 'waiting_input'].includes(this.runLifecycle) || !this.sessionId) return;
      const contextToken = this.sessionContextToken;
      const sessionId = this.sessionId;
      this._setRunLifecycle('cancelling');
      this.statusNotice = '正在取消生成…';
      this.eventSource?.close();
      this.eventSource = null;
      this._stopSseRecoveryPoll();

      try {
        let executionId = this.executionId;
        if (!executionId) {
          const session = await api.session(sessionId);
          executionId = session.latest_execution?.id ?? '';
        }
        if (!executionId) throw new Error('尚未获取到可取消的任务，请稍后重试。');

        const execution = await api.cancelRun(executionId);
        if (!this._isConversationContextCurrent(contextToken) || this.sessionId !== sessionId) return;
        this.executionId = execution.id;
        this._setExecutionInfo(execution);

        for (let attempt = 0; attempt < 20; attempt += 1) {
          const latest = await api.session(sessionId);
          if (!this._isConversationContextCurrent(contextToken) || this.sessionId !== sessionId) return;
          const latestExecution = latest.latest_execution;
          this.sessionInfo = latest;
          this._setExecutionInfo(latestExecution);
          this.executionId = latestExecution?.id ?? executionId;
          const lifecycle = toRunLifecycle(latestExecution?.status);
          if (!['queued', 'running', 'reconnecting'].includes(lifecycle)) {
            this._setRunLifecycle(lifecycle);
            this.statusNotice = lifecycle === 'cancelled' ? '已停止生成。' : '';
            this._preserveStreamingAssistantContent();
            await this.refreshSessions().catch(() => undefined);
            return;
          }
          await new Promise<void>((resolve) => window.setTimeout(resolve, 250));
        }
        this.statusNotice = '取消请求已发送，正在等待任务停止。';
      } catch (error) {
        if (!this._isConversationContextCurrent(contextToken)) return;
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        this.error = normalizeErrorMessage(error);
        this.statusNotice = '';
        this._setRunLifecycle('running');
      }
    },

    listen(sessionId: string, contextToken?: number, sourceOverride?: FetchEventStream) {
      const resolvedContextToken = contextToken ?? this.sessionContextToken;
      if (!this._isConversationContextCurrent(resolvedContextToken)) return;
      this.activeSessionId = sessionId;
      const isActiveContext = () =>
        this._isConversationContextCurrent(resolvedContextToken) && this.activeSessionId === sessionId;
      this.eventSource?.close();
      this.eventSource = null;
      this._stopSseRecoveryPoll(false);

      if (!sourceOverride) {
        this._scheduleSseRecovery(sessionId, resolvedContextToken);
        return;
      }
      const source = sourceOverride;
      this.eventSource = source;
      let terminalEventReceived = false;

      const completeRun = async () => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('completed');
        await this.refreshSession();
        if (!isActiveContext()) return;
        await this.refreshSessions().catch(() => undefined);
        if (!isActiveContext()) return;
        this._removeActiveStreamingAssistantPlaceholder();
        source.close();
        this.eventSource = null;
      };

      const cancelRun = async () => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('cancelled');
        this._preserveStreamingAssistantContent();
        await this.refreshSession(false);
        if (!isActiveContext()) return;
        await this.refreshSessions().catch(() => undefined);
        if (!isActiveContext()) return;
        source.close();
        this.eventSource = null;
      };

      const waitForInput = async (name: string, data: Record<string, unknown>) => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('waiting_input');
        this._recordTimelineEvent(name || 'waiting_input', data);
        await this.refreshSession();
        if (!isActiveContext()) return;
        await this.refreshSessions().catch(() => undefined);
        if (!isActiveContext()) return;
        this._removeActiveStreamingAssistantPlaceholder();
        source.close();
        this.eventSource = null;
      };

      const failRun = async (data: Record<string, unknown>) => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('failed');
        this._recordTimelineEvent('error', data);
        const message = typeof data.message === 'string'
          ? data.message
          : typeof data.error === 'string'
            ? data.error
            : typeof data.content === 'string'
              ? data.content
              : typeof data.code === 'string'
                ? `错误码：${data.code}`
                : typeof data.error_code === 'string'
                  ? `错误码：${data.error_code}`
                  : '执行失败';
        this.error = formatStreamErrorMessage(message, data);
        await this.refreshSession();
        if (!isActiveContext()) return;
        await this.refreshSessions().catch(() => undefined);
        if (!isActiveContext()) return;
        this._removeActiveStreamingAssistantPlaceholder();
        source.close();
        this.eventSource = null;
      };

      const handleMessageEvent = async (event: MessageEvent) => {
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, event.type || 'messages', event)) return;
        const data = this._parseStreamEventData(event);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        const name = typeof data.name === 'string' ? data.name : '';
        const content = typeof data.content === 'string' ? data.content : '';
        const chunk = typeof data.chunk === 'string' ? data.chunk : '';
        const messageType = (
          typeof data.message_type === 'string' &&
          (data.message_type === 'text' ||
            data.message_type === 'markdown' ||
            data.message_type === 'json')
        ) ? data.message_type : undefined;
        const done = Boolean(data.done);
        this.statusNotice = '';
        this._setRunLifecycle('running');

        if (name === 'assistant_message_delta') {
          this._startStreamingAssistantMessage();
          this._appendAssistantMessageDelta(content || chunk, done, messageType || 'markdown');
          return;
        }

        if (name === 'assistant_message') {
          this._startStreamingAssistantMessage();
          if (content) {
            if (this.isStreamingAssistantMessage && !chunk) {
              this._appendAssistantMessageDelta(content, done, messageType || 'markdown');
            } else {
              this._appendAssistantMessage(content, messageType || 'text');
            }
          }
          return;
        }

        this._recordTimelineEvent(name || 'messages', data);
      };

      const handleLifecycleEvent = async (event: MessageEvent) => {
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, event.type, event)) return;
        const data = this._parseStreamEventData(event);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        const name = typeof data.name === 'string' ? data.name : '';
        const executionId = typeof data.execution_id === 'string' ? data.execution_id : '';
        if (executionId) this.executionId = executionId;

        if (
          name === 'run_start' || name === 'run_retry' || name === 'run_resume' ||
          name === 'attempt_start'
        ) {
          this._setRunLifecycle('running');
          this._startStreamingAssistantMessage();
          this._recordTimelineEvent(name, data);
          return;
        }

        if (name === 'run_finish') {
          await completeRun();
          return;
        }

        if (name === 'run_cancel') {
          await cancelRun();
          return;
        }

        if (name === 'run_error') {
          await failRun(data);
          return;
        }

        if (
          name === 'run_interrupt'
        ) {
          await waitForInput(name, data);
          return;
        }

        if (['queued', 'reconnecting'].includes(this.runLifecycle)) {
          this._setRunLifecycle('running');
        }
        this._recordTimelineEvent(name || 'lifecycle', data);
      };

      const handleErrorEvent = async (event: MessageEvent) => {
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, 'errors', event)) return;
        const data = this._parseStreamEventData(event);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        const code = typeof data.code === 'string'
          ? data.code
          : typeof data.error_code === 'string'
            ? data.error_code
            : '';
        if (STREAM_DEGRADED_CODES.has(code)) {
          terminalEventReceived = true;
          source.close();
          this.eventSource = null;
          this._setRunLifecycle('reconnecting');
          this.statusNotice = '实时更新已降级，正在每 3 秒刷新任务状态…';
          this._startRunStatusPolling(sessionId, resolvedContextToken);
          return;
        }
        await failRun(data);
      };

      const handleToolEvent = (event: MessageEvent) => {
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, event.type, event)) return;
        const data = this._parseStreamEventData(event);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        this._setRunLifecycle('running');
        const name = typeof data.name === 'string' && data.name ? data.name : event.type;
        this._recordTimelineEvent(name, data);
      };

      source.addEventListener('messages', (event) => {
        void handleMessageEvent(event as MessageEvent);
      });

      source.addEventListener('tools', (event) => {
        handleToolEvent(event as MessageEvent);
      });

      source.addEventListener('lifecycle', (event) => {
        void handleLifecycleEvent(event as MessageEvent);
      });

      source.addEventListener('interrupts', (event) => {
        const messageEvent = event as MessageEvent;
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, 'interrupts', messageEvent)) return;
        const data = this._parseStreamEventData(messageEvent);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        const name = typeof data.name === 'string' ? data.name : 'run_interrupt';
        void waitForInput(name, data);
      });

      source.addEventListener('errors', (event) => {
        void handleErrorEvent(event as MessageEvent);
      });

      source.addEventListener('values', (event) => {
        const messageEvent = event as MessageEvent;
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, 'values', messageEvent)) return;
        const data = this._parseStreamEventData(messageEvent);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        const name = typeof data.name === 'string' ? data.name : 'values';
        this._recordTimelineEvent(name, data);
      });

      source.addEventListener('close', async () => {
        if (!isActiveContext()) return;
        if (!terminalEventReceived) {
          terminalEventReceived = true;
          await this._recoverFromSseRunState(sessionId, true);
        }
        source.close();
        this.eventSource = null;
      });

      source.onerror = async (error) => {
        if (!isActiveContext() || terminalEventReceived) return;
        terminalEventReceived = true;
        source.close();
        this.eventSource = null;
        this._setRunLifecycle('reconnecting');
        this.statusNotice = '实时连接中断，正在恢复运行状态…';
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        await this._recoverFromSseRunState(sessionId, true);
      };
    },

    _setRunLifecycle(lifecycle: RunLifecycle) {
      this.runLifecycle = lifecycle;
      if (lifecycle === 'queued') {
        this.status = 'pending';
      } else if (lifecycle === 'running' || lifecycle === 'reconnecting') {
        this.status = 'running';
      } else {
        this.status = lifecycle;
      }
    },


    _startStreamingAssistantMessage() {
      const index = this._findActiveAssistantPlaceholderIndex();
      if (index >= 0) {
        const assistant = this.messages[index];
        if (assistant.role === 'assistant') {
          assistant.assistant_state = 'streaming';
          this.assistantStreamingState = 'streaming';
          this.isStreamingAssistantMessage = true;
          this.streamingAssistantMessageIndex = index;
          this.assistantStreamingBuffer = assistant.content || '';
        }
        return;
      }
      this.messages.push({
        role: 'assistant',
        content: '',
        message_type: 'markdown',
        assistant_state: 'streaming'
      });
      this.assistantStreamingState = 'streaming';
      this.isStreamingAssistantMessage = true;
      this.streamingAssistantMessageIndex = this.messages.length - 1;
      this.assistantStreamingBuffer = '';
    },

    _ensureAssistantPlaceholder() {
      const index = this._findActiveAssistantPlaceholderIndex();
      if (index >= 0) {
        const assistant = this.messages[index];
        if (assistant.role === 'assistant') {
          assistant.assistant_state = 'pending';
          assistant.assistant_state = assistant.assistant_state || 'pending';
          this.assistantStreamingState = assistant.assistant_state;
          this.assistantStreamingBuffer = assistant.content || '';
          this.isStreamingAssistantMessage = true;
          this.streamingAssistantMessageIndex = index;
        }
        return;
      }
      this.messages.push({
        role: 'assistant',
        content: '',
        message_type: 'markdown',
        assistant_state: 'pending'
      });
      this.assistantStreamingState = 'pending';
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = true;
      this.streamingAssistantMessageIndex = this.messages.length - 1;
    },

    _removeActiveStreamingAssistantPlaceholder() {
      const indexesToRemove: number[] = [];
      for (let index = 0; index < this.messages.length; index++) {
        const message = this.messages[index];
        if (message.role === 'assistant' && message.assistant_state && message.assistant_state !== 'normal') {
          indexesToRemove.push(index);
        }
      }
      if (!indexesToRemove.length) {
        this.assistantStreamingState = 'normal';
        this.assistantStreamingBuffer = '';
        this.isStreamingAssistantMessage = false;
        this.streamingAssistantMessageIndex = -1;
        return;
      }
      for (let i = indexesToRemove.length - 1; i >= 0; i--) {
        const index = indexesToRemove[i];
        this.messages.splice(index, 1);
      }
      this.assistantStreamingState = 'normal';
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
    },

    _preserveStreamingAssistantContent() {
      const indexesToRemove: number[] = [];
      for (let index = 0; index < this.messages.length; index += 1) {
        const message = this.messages[index];
        if (message.role !== 'assistant' || !message.assistant_state || message.assistant_state === 'normal') continue;
        if (message.content.trim()) {
          message.assistant_state = 'normal';
        } else {
          indexesToRemove.push(index);
        }
      }
      for (let index = indexesToRemove.length - 1; index >= 0; index -= 1) {
        this.messages.splice(indexesToRemove[index], 1);
      }
      this.assistantStreamingState = 'normal';
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
    },

    _hasSeenSseEvent(scopeId: string, eventName: string, event: MessageEvent) {
      const rawId = event.lastEventId;
      const sequence = streamEventSequence(event);
      if (Number.isInteger(sequence) && sequence > this.lastEventSequence) {
        this.lastEventSequence = sequence;
      }
      if (!rawId) return false;
      const key = `${scopeId}:${eventName}:${rawId}`;
      if (this.processedSseEventIds.includes(key)) {
        return true;
      }
      this.processedSseEventIds.push(key);
      this.processedSseEventIds = this.processedSseEventIds.slice(-400);
      return false;
    },

    _parseStreamEventData(event: MessageEvent) {
      try {
        const eventEnvelope = JSON.parse(event.data) as unknown;
        if (!eventEnvelope || typeof eventEnvelope !== 'object') return null;
        const envelope = eventEnvelope as Record<string, unknown>;
        const data = envelope.data && typeof envelope.data === 'object'
          ? { ...(envelope.data as Record<string, unknown>) }
          : {};
        if (typeof envelope.code === 'string') data.code = envelope.code;
        if (typeof envelope.error_code === 'string') data.error_code = envelope.error_code;
        if (typeof envelope.request_id === 'string') data.request_id = envelope.request_id;
        if (typeof envelope.session_id === 'string') data.session_id = envelope.session_id;
        if (typeof envelope.thread_id === 'string') data.thread_id = envelope.thread_id;
        if (typeof envelope.execution_id === 'string') data.execution_id = envelope.execution_id;
        if (typeof envelope.tool_name === 'string') data.tool_name = envelope.tool_name;
        if (typeof envelope.tool_call_id === 'string') data.tool_call_id = envelope.tool_call_id;
        if (typeof envelope.type === 'string') data.event_type = envelope.type;
        return data;
      } catch {
        return null;
      }
    },

    _isActiveRunPayload(sessionId: string, data: Record<string, unknown>, contextToken: number) {
      if (!this._isConversationContextCurrent(contextToken)) return false;
      if (this.activeSessionId !== sessionId) return false;
      const payloadSessionId = typeof data.session_id === 'string' ? data.session_id : '';
      if (payloadSessionId && payloadSessionId !== sessionId) return false;
      const executionId = typeof data.execution_id === 'string' ? data.execution_id : '';
      return !this.executionId || !executionId || executionId === this.executionId;
    },

    _setExecutionInfo(execution: ChatExecutionInfo | null) {
      this.executionInfo = execution;
      this.pendingInterrupt = execution?.status === 'waiting_input'
        ? publicInterrupt(execution.interrupt)
        : null;
    },

    async refreshSession(hydrateMessages = true) {
      if (!this.sessionId) return;
      const contextToken = this.sessionContextToken;
      const sessionInfo = await api.session(this.sessionId);
      if (!this._isConversationContextCurrent(contextToken)) return;
      this.sessionInfo = sessionInfo;
      this._setExecutionInfo(this.sessionInfo.latest_execution);
      this.executionId = this.executionInfo?.id ?? '';
      const latestStatus = resolveSessionStatus(this.sessionInfo, 'idle');
      this.status = latestStatus;
      this._setRunLifecycle(toRunLifecycle(latestStatus));
      this.statusNotice = '';
      if (hydrateMessages) {
        await this._hydrateMessages(this.sessionInfo);
      }
      if (!['queued', 'running', 'reconnecting'].includes(this.runLifecycle)) {
        this._removeActiveStreamingAssistantPlaceholder();
        return;
      }
      if (['queued', 'running', 'reconnecting'].includes(this.runLifecycle)) {
        this._ensureAssistantPlaceholder();
      }
    },

    _appendAssistantMessage(
      content: string,
      messageType: WorkbenchMessage['message_type'] = 'text'
    ) {
      if (!content && content !== '') return;
      const streamingIndex = this._findActiveAssistantPlaceholderIndex();
      if (streamingIndex >= 0) {
        const target = this.messages[streamingIndex];
        if (target && target.role === 'assistant') {
          target.content = content;
          target.message_type = messageType;
          target.assistant_state = 'normal';
          this.assistantStreamingBuffer = content;
          this._clearStreamingAssistantMessage();
          this._setRunLifecycle('running');
          return;
        }
      }
      if (this.isStreamingAssistantMessage && this.streamingAssistantMessageIndex >= 0) {
        const target = this.messages[this.streamingAssistantMessageIndex];
        if (target && target.role === 'assistant') {
          target.content = content;
          target.message_type = messageType;
          target.assistant_state = 'normal';
          this.assistantStreamingBuffer = content;
          this._clearStreamingAssistantMessage();
          return;
        }
      }
      if (this.messages.length) {
        const latest = this.messages[this.messages.length - 1];
        if (latest.role === 'assistant' && (latest.content === content || latest.content.includes(content.slice(0, 40)))) {
          latest.content = content;
          latest.message_type = messageType;
          latest.assistant_state = 'normal';
          this._clearStreamingAssistantMessage();
          this.assistantStreamingBuffer = content;
          return;
        }
      }
      this.messages.push({ role: 'assistant', content, message_type: messageType, assistant_state: 'normal' });
      this.assistantStreamingBuffer = content;
      this._clearStreamingAssistantMessage();
      this._setRunLifecycle('running');
    },

    _appendAssistantMessageDelta(
      chunk: string,
      done: boolean,
      messageType: WorkbenchMessage['message_type'] = 'markdown'
    ) {
      if (!chunk) {
        if (done) {
          this._setRunLifecycle('running');
        }
        return;
      }
      const streamingIndex = this._findActiveAssistantPlaceholderIndex();
      if (streamingIndex >= 0) {
        const target = this.messages[streamingIndex];
        if (target && target.role === 'assistant') {
          this.assistantStreamingBuffer += chunk;
          target.content = this.assistantStreamingBuffer;
          target.message_type = messageType;
          target.assistant_state = 'streaming';
          this.assistantStreamingState = 'streaming';
          this.isStreamingAssistantMessage = true;
          this.streamingAssistantMessageIndex = streamingIndex;
          return;
        }
      }
      this.messages.push({
        role: 'assistant',
        content: chunk,
        message_type: messageType,
        assistant_state: 'streaming'
      });
      this.assistantStreamingBuffer = chunk;
      this.assistantStreamingState = 'streaming';
      this.isStreamingAssistantMessage = true;
      this.streamingAssistantMessageIndex = this.messages.length - 1;
    },

    _clearStreamingAssistantMessage() {
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
      this.assistantStreamingState = 'normal';
    },

    async _recoverFromSseRunState(sessionId: string, connectionError = false) {
      const contextToken = this.sessionContextToken;
      if (!this.executionId) return;
      const execution = await api.runStatus(this.executionId).catch(() => null);
      if (!this._isConversationContextCurrent(contextToken) || this.sessionId !== sessionId) return;
      if (execution) {
        this._setExecutionInfo(execution);
        this.executionId = execution.id;
      }
      const status = execution?.status ?? this.executionInfo?.status;
      const runLifecycle = toRunLifecycle(status);
      if (runLifecycle === 'queued' || runLifecycle === 'running') {
        if (execution?.streaming_degraded) {
          this._setRunLifecycle('reconnecting');
          this.statusNotice = '实时更新已降级，正在每 3 秒刷新任务状态…';
          this._startRunStatusPolling(sessionId, contextToken);
          return;
        }
        if (connectionError) {
          this._setRunLifecycle('reconnecting');
          this.statusNotice = '实时连接中断，正在从上次位置恢复…';
          this._scheduleSseRecovery(sessionId, contextToken);
        } else {
          this._setRunLifecycle(runLifecycle);
        }
        return;
      }
      this._stopSseRecoveryPoll();
      await this.refreshSession().catch(() => undefined);
      if (!this._isConversationContextCurrent(contextToken) || this.sessionId !== sessionId) return;
      if (runLifecycle === 'failed') {
        this._setRunLifecycle('failed');
        const executionError = this.executionInfo?.error;
        this.error = normalizeErrorMessage(
          typeof executionError === 'object' && executionError?.message
            ? executionError.message
            : executionError || '执行失败'
        );
        return;
      }
      if (connectionError && !status) {
        this.error = '会话异常，请重试';
        this._setRunLifecycle('failed');
      }
      this._removeActiveStreamingAssistantPlaceholder();
    },

    async _hydrateMessages(session: ChatSessionDetail, preserveCursor = true) {
      const mapped: WorkbenchMessage[] = session.messages
        .filter((message) => ['text', 'markdown'].includes(message.message_type))
        .map((message) => ({
          id: message.id,
          role: message.role as WorkbenchMessage['role'],
          content: message.content,
          message_type: message.message_type,
          assistant_state: 'normal' as AssistantBubbleState
        }));
      const merged = [...this.messages];
      for (const message of mapped) {
        const sameId = message.id ? merged.findIndex((item) => item.id === message.id) : -1;
        if (sameId >= 0) {
          merged.splice(sameId, 1, message);
          continue;
        }
        const optimistic = !message.id
          ? -1
          : merged.findIndex((item) => !item.id && item.role === message.role && item.content === message.content);
        if (optimistic >= 0) merged.splice(optimistic, 1, message);
        else merged.push(message);
      }
      this.messages = merged;
      if (!preserveCursor) this.messageNextCursor = session.next_cursor;
    },

    _stopSseRecoveryPoll(resetAttempts = true) {
      if (this.ssePollTimer) {
        window.clearTimeout(this.ssePollTimer);
        this.ssePollTimer = null;
      }
      if (resetAttempts) {
        this.sseRecoveryAttempts = 0;
      }
    },

    _scheduleSseRecovery(sessionId: string, contextToken?: number) {
      const resolvedContextToken = contextToken ?? this.sessionContextToken;
      if (this.ssePollTimer) return;
      if (this.sseRecoveryAttempts >= SSE_RECOVERY_MAX_ATTEMPTS) {
        this.statusNotice = '实时连接持续失败，已切换为每 3 秒刷新任务状态…';
        this._startRunStatusPolling(sessionId, resolvedContextToken);
        return;
      }
      const delay = Math.min(
        SSE_RECOVERY_INITIAL_DELAY_MS * 2 ** this.sseRecoveryAttempts,
        SSE_RECOVERY_MAX_DELAY_MS
      );
      const poll = async () => {
        if (!this._isConversationContextCurrent(resolvedContextToken) || this.sessionId !== sessionId) {
          this.ssePollTimer = null;
          return;
        }
        if (this.executionId) {
          const execution = await api.runStatus(this.executionId).catch(() => null);
          if (execution) {
            this._setExecutionInfo(execution);
            const lifecycle = toRunLifecycle(execution.status);
            if (!['queued', 'running'].includes(lifecycle)) {
              this.ssePollTimer = null;
              this._setRunLifecycle(lifecycle);
              await this.refreshSession();
              if (
                this._isConversationContextCurrent(resolvedContextToken) &&
                this.sessionId === sessionId
              ) {
                await this.refreshSessions().catch(() => undefined);
                this._removeActiveStreamingAssistantPlaceholder();
              }
              return;
            }
            if (execution.streaming_degraded) {
              this.ssePollTimer = null;
              this._setRunLifecycle('reconnecting');
              this.statusNotice = '实时更新已降级，正在每 3 秒刷新任务状态…';
              this._startRunStatusPolling(sessionId, resolvedContextToken);
              return;
            }
            this._setRunLifecycle(lifecycle);
            this.statusNotice = queueStageNotice(execution);
          }
        }
        this.ssePollTimer = null;
        if (
          this._isConversationContextCurrent(resolvedContextToken) &&
          ['queued', 'running', 'reconnecting'].includes(this.runLifecycle) && this.executionId
        ) {
          this.sseRecoveryAttempts += 1;
          this.listen(
            sessionId,
            resolvedContextToken,
            api.executionEvents(this.executionId, this.lastEventSequence)
          );
        }
      };
      this.ssePollTimer = window.setTimeout(poll, delay);
    },

    _startRunStatusPolling(sessionId: string, contextToken?: number) {
      const resolvedContextToken = contextToken ?? this.sessionContextToken;
      this._stopSseRecoveryPoll(false);
      const poll = async () => {
        if (
          !this._isConversationContextCurrent(resolvedContextToken) ||
          this.sessionId !== sessionId ||
          !this.executionId
        ) {
          this.ssePollTimer = null;
          return;
        }
        const execution = await api.runStatus(this.executionId).catch(() => null);
        this.ssePollTimer = null;
        if (!this._isConversationContextCurrent(resolvedContextToken) || this.sessionId !== sessionId) {
          return;
        }
        if (!execution) {
          this.ssePollTimer = window.setTimeout(poll, RUN_STATUS_POLL_MS);
          return;
        }
        this._setExecutionInfo(execution);
        this.executionId = execution.id;
        const lifecycle = toRunLifecycle(execution.status);
        if (lifecycle === 'queued' || lifecycle === 'running') {
          this._setRunLifecycle(execution.streaming_degraded ? 'reconnecting' : lifecycle);
          this.statusNotice = execution.streaming_degraded
            ? '实时更新已降级，正在每 3 秒刷新任务状态…'
            : queueStageNotice(execution);
          this.ssePollTimer = window.setTimeout(poll, RUN_STATUS_POLL_MS);
          return;
        }
        this._setRunLifecycle(lifecycle);
        this.statusNotice = lifecycle === 'cancelled' ? '已停止生成。' : '';
        await this.refreshSession();
        if (!this._isConversationContextCurrent(resolvedContextToken) || this.sessionId !== sessionId) {
          return;
        }
        await this.refreshSessions().catch(() => undefined);
        this._removeActiveStreamingAssistantPlaceholder();
      };
      void poll();
    },

    _recordTimelineEvent(eventName: string, data: Record<string, unknown>) {
      this.events.push({ event: eventName, data });
      this.events = this.events.slice(-200);
    },

    _nextIdempotencyKey() {
      const hasNativeRandom = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function';
      const randomPart = hasNativeRandom
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
      return `client-${randomPart}`;
    }
  }
});


