import { defineStore } from 'pinia';
import { markRaw } from 'vue';
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
  type RunMessage,
  type ResumeDecision
} from '@/shared/services/api';

const SSE_RECOVERY_INITIAL_DELAY_MS = 1000;
const SSE_RECOVERY_MAX_DELAY_MS = 10000;
const SSE_RECOVERY_MAX_ATTEMPTS = 8;
const RUN_STATUS_POLL_MS = 3000;
const SSE_DEDUPE_MAX_ENTRIES = 400;
const SSE_DEDUPE_MAX_CHARACTERS = 2 * 1024 * 1024;
const AGENT_PAGE_LIMIT = 200;
const AGENT_CATALOG_MAX_PAGES = 100;
const AGENT_CATALOG_MAX_ITEMS = 20_000;
const SESSION_MESSAGE_PAGE_LIMIT = 200;
const SESSION_HISTORY_MAX_PAGES = 100;
const SESSION_HISTORY_MAX_MESSAGES = 20_000;
const STREAM_DEGRADED_CODES = new Set([
  'STREAMING_DEGRADED',
  'STREAM_REPLAY_GAP',
  'STREAM_REPLAY_EXPIRED',
  'INVALID_STREAM_CURSOR',
  'REDIS_READ_FAILED',
  'STREAM_EXCEPTION_ERROR'
]);
const SSE_CHANNELS = new Set([
  'messages',
  'tools',
  'values',
  'lifecycle',
  'interrupts',
  'errors'
]);
const SSE_RESERVED_DATA_KEYS = new Set([
  'request_id',
  'session_id',
  'thread_id',
  'execution_id',
  'sequence',
  'event_id',
  'channel',
  'namespace',
  'attempt_id',
  'message_id',
  'tool_call_id',
  'timestamp',
  'schema_version',
  'event_type'
]);

type SseFrameResult =
  | { kind: 'accepted' | 'control'; data: Record<string, unknown> }
  | { kind: 'duplicate' | 'invalid' };

export class AgentCatalogRefreshCancelledError extends Error {
  constructor() {
    super('Agent catalog refresh was cancelled.');
    this.name = 'AgentCatalogRefreshCancelledError';
  }
}

export type AssistantBubbleState = 'normal' | 'pending' | 'streaming';
export type RunLifecycle = 'idle' | 'queued' | 'running' | 'reconnecting' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'waiting_input';
type TerminalRunLifecycle = Extract<
  RunLifecycle,
  'completed' | 'failed' | 'cancelled' | 'waiting_input'
>;

function isTerminalRunLifecycle(
  lifecycle: RunLifecycle
): lifecycle is TerminalRunLifecycle {
  return lifecycle === 'completed' ||
    lifecycle === 'failed' ||
    lifecycle === 'cancelled' ||
    lifecycle === 'waiting_input';
}

export interface TimelineEvent {
  event: string;
  data: Record<string, unknown>;
}

export type WorkbenchMessage = {
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  message_type: 'text' | 'markdown' | 'json';
  created_at?: string;
  assistant_state?: AssistantBubbleState;
};

function compareDatedMessages(
  left: Pick<RunMessage, 'id' | 'created_at'>,
  right: Pick<RunMessage, 'id' | 'created_at'>
) {
  const leftTime = Date.parse(left.created_at);
  const rightTime = Date.parse(right.created_at);
  if (Number.isFinite(leftTime) && Number.isFinite(rightTime)) {
    return leftTime === rightTime ? left.id.localeCompare(right.id) : leftTime - rightTime;
  }
  const timestampOrder = left.created_at.localeCompare(right.created_at);
  return timestampOrder || left.id.localeCompare(right.id);
}

function messageSignature(message: Pick<WorkbenchMessage, 'role' | 'content' | 'message_type'>) {
  return `${message.role}\u0000${message.message_type}\u0000${message.content}`;
}

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isTimezoneAwareIsoTimestamp(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/
  );
  if (!match) return false;
  const [, yearRaw, monthRaw, dayRaw, hourRaw, minuteRaw, secondRaw, offsetHourRaw, offsetMinuteRaw] = match;
  const year = Number(yearRaw);
  const month = Number(monthRaw);
  const day = Number(dayRaw);
  const hour = Number(hourRaw);
  const minute = Number(minuteRaw);
  const second = Number(secondRaw);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysInMonth[month - 1] ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) return false;
  if (
    offsetHourRaw !== undefined &&
    (Number(offsetHourRaw) > 23 || Number(offsetMinuteRaw) > 59)
  ) return false;
  return Number.isFinite(Date.parse(value));
}

function isNullableString(value: unknown) {
  return value === null || typeof value === 'string';
}

function isCanonicalNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value) && value.trim() === value;
}

function isMessageType(value: unknown) {
  return value === 'text' || value === 'markdown' || value === 'json';
}

function isPublicInterrupt(value: unknown): value is PublicInterrupt {
  if (!isRecord(value)) return false;
  if (typeof value.interrupt_id !== 'string' || !value.interrupt_id.trim()) return false;
  if (!Array.isArray(value.actions) || value.actions.length === 0) return false;
  return value.actions.every((action) => {
    if (!isRecord(action)) return false;
    if (typeof action.tool_name !== 'string' || !action.tool_name.trim()) return false;
    if (typeof action.purpose !== 'string' || !action.purpose.trim()) return false;
    if (action.memory === undefined || action.memory === null) return true;
    return isRecord(action.memory) &&
      typeof action.memory.type === 'string' &&
      Boolean(action.memory.type.trim()) &&
      typeof action.memory.content === 'string' &&
      Boolean(action.memory.content.trim());
  });
}

function hasValidSemanticSseData(
  channel: string,
  data: Record<string, unknown>,
  sequenced: boolean
) {
  const name = typeof data.name === 'string' ? data.name : '';
  if (channel === 'messages' && name === 'assistant_message_delta') {
    const hasContent = typeof data.content === 'string';
    const hasChunk = typeof data.chunk === 'string';
    if (!hasContent && !hasChunk) return false;
    if (data.content !== undefined && !hasContent) return false;
    if (data.chunk !== undefined && !hasChunk) return false;
    if (data.done !== undefined && typeof data.done !== 'boolean') return false;
    return data.message_type === undefined || isMessageType(data.message_type);
  }
  if (channel === 'messages' && name === 'assistant_message') {
    return typeof data.content === 'string' &&
      (data.message_type === undefined || isMessageType(data.message_type));
  }
  if (channel === 'errors' && sequenced) {
    return typeof data.code === 'string' && Boolean(data.code.trim()) &&
      typeof data.message === 'string' && Boolean(data.message.trim());
  }
  if (channel === 'interrupts') {
    return (name === 'run_interrupt' || name === 'execution_waiting_input') &&
      isPublicInterrupt(data.interrupt);
  }
  if (name === 'run_interrupt' || name === 'execution_waiting_input') {
    return false;
  }
  return true;
}

export const useWorkbenchStore = defineStore('workbench', {
  state: () => ({
    agents: [] as AgentProfile[],
    sessions: [] as ChatSessionSummary[],
    agentId: '',
    sessionId: '',
    executionId: '',
    activeAttemptId: '',
    activeSessionId: '',
    status: 'idle',
    messages: [] as WorkbenchMessage[],
    events: [] as TimelineEvent[],
    sessionInfo: null as ChatSessionDetail | null,
    executionInfo: null as ChatExecutionInfo | null,
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
    ssePollEpoch: 0,
    processedSseEventIds: [] as string[],
    processedSseEventCharacterCount: 0,
    lastEventSequence: 0,
    eventCursorExecutionId: '',
    cursorKnown: false,
    streamGeneration: 0,
    runActionEpoch: 0,
    sessionContextToken: 0,
    agentRefreshGeneration: 0,
    agentRefreshController: null as AbortController | null,
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
          state.executionId &&
          state.executionInfo?.interrupt?.interrupt_id &&
          (state.status === 'waiting_input' || state.runLifecycle === 'waiting_input')
      );
    },
    interrupt(state) {
      return state.executionInfo?.interrupt ?? null;
    }
  },
  actions: {
    async boot() {
      if (!(await this.refreshAgents())) return;
      if (!this.agentId) {
        this.sessions = [];
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

    async refreshSessions(guard?: () => boolean) {
      if (guard && !guard()) return;
      const requestedAgentId = this.agentId;
      const allSessions: ChatSessionSummary[] = [];
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      do {
        const response = await api.sessions({
          agent_id: requestedAgentId || undefined,
          cursor,
          limit: 200
        });
        allSessions.push(...response.items);
        const nextCursor = response.next_cursor?.trim() || '';
        if (!nextCursor || seenCursors.has(nextCursor)) break;
        seenCursors.add(nextCursor);
        cursor = nextCursor;
      } while (true);
      if (this.agentId !== requestedAgentId || (guard && !guard())) return;
      this.sessions = this.agentId
        ? allSessions.filter((session) => session.agent_id === this.agentId)
        : allSessions;
    },

    async chooseAgent(agentId: string, force = false) {
      if (!force && this.agentId === agentId && this.sessionInfo?.agent_id === agentId) return true;
      const contextToken = this._beginConversationContext();
      this.agentId = agentId;
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
      if (!this._isConversationContextCurrent(contextToken)) return;

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

    async _loadSessionHistory(
      sessionId: string,
      contextToken: number
    ): Promise<ChatSessionDetail | null> {
      const messagesById = new Map<string, RunMessage>();
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      let latestPage: ChatSessionDetail | null = null;

      for (let pageNumber = 0; pageNumber < SESSION_HISTORY_MAX_PAGES; pageNumber += 1) {
        if (!this._isConversationContextCurrent(contextToken)) return null;
        const page = await api.session(sessionId, {
          cursor,
          limit: SESSION_MESSAGE_PAGE_LIMIT
        });
        if (!this._isConversationContextCurrent(contextToken)) return null;
        if (page.session_id !== sessionId) {
          throw new Error('Session history response does not match the requested session.');
        }
        latestPage = page;

        for (const message of page.messages) {
          if (messagesById.has(message.id)) continue;
          if (messagesById.size >= SESSION_HISTORY_MAX_MESSAGES) {
            throw new Error('Session history exceeds the safe loading limit.');
          }
          messagesById.set(message.id, message);
        }

        const nextCursor = page.next_cursor?.trim() || '';
        if (!nextCursor) {
          return {
            ...latestPage,
            messages: [...messagesById.values()].sort(compareDatedMessages),
            next_cursor: null
          };
        }
        if (seenCursors.has(nextCursor)) {
          throw new Error('Session history pagination returned a repeated cursor.');
        }
        seenCursors.add(nextCursor);
        cursor = nextCursor;
      }

      throw new Error('Session history exceeds the safe page limit.');
    },

    async loadSession(sessionId: string): Promise<boolean> {
      if (!sessionId || (sessionId === this.sessionId && this.sessionInfo)) return true;
      const contextToken = this._beginConversationContext();
      const requestedAgentId = this.agentId;
      this.isLoadingSession = true;
      if (!this._isConversationContextCurrent(contextToken)) return false;

      try {
        const session = await this._loadSessionHistory(sessionId, contextToken);
        if (!session || !this._isConversationContextCurrent(contextToken)) return false;
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
        // Messages can arrive from an already-started realtime run while older
        // history pages are still loading. Merge those local additions instead
        // of replacing the array with the paginated snapshot.
        this._hydrateMessages(session, true);
        this._setExecutionScope(session.latest_execution?.id ?? '');
        this.activeSessionId = '';
        const resolvedStatus = resolveSessionStatus(session, 'idle');
        this.status = resolvedStatus;
        this.runLifecycle = toRunLifecycle(resolvedStatus);
        this.assistantStreamingState = 'normal';

        this.sessionInfo = session;
        this.executionInfo = session.latest_execution;
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
        if (
          this._isConversationContextCurrent(contextToken) &&
          this.agentId === requestedAgentId
        ) {
          this.lastErrorCode = error instanceof ApiError ? error.code : '';
          this.error = normalizeErrorMessage(error);
        }
        return false;
      } finally {
        if (
          this._isConversationContextCurrent(contextToken) &&
          this.agentId === requestedAgentId
        ) {
          this.isLoadingSession = false;
        }
      }
    },

    resetConversationContext() {
      this.runActionEpoch += 1;
      this._invalidateActiveStream();
      this.sessionId = '';
      this._setExecutionScope('');
      this.activeSessionId = '';
      this.messages = [];
      this.events = [];
      this.sessionInfo = null;
      this.executionInfo = null;
      this.error = '';
      this.lastErrorCode = '';
      this.statusNotice = '';
      this.status = 'idle';
      this.runLifecycle = 'idle';
      this.assistantStreamingState = 'normal';
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
      this._stopSseRecoveryPoll();
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

    _beginRunAction() {
      this.runActionEpoch += 1;
      return this.runActionEpoch;
    },

    _isRunActionCurrent(
      actionEpoch: number,
      contextToken: number,
      sessionId: string,
      executionId?: string
    ) {
      return this.runActionEpoch === actionEpoch &&
        this._isConversationContextCurrent(contextToken) &&
        this.sessionId === sessionId &&
        (executionId === undefined || this.executionId === executionId);
    },

    _invalidateActiveStream() {
      this.streamGeneration += 1;
      this.eventSource?.close();
      this.eventSource = null;
      this._stopSseRecoveryPoll(false);
      return this.streamGeneration;
    },

    _isStreamGenerationCurrent(generation: number) {
      return this.streamGeneration === generation;
    },

    _isExpectedExecutionResponse(
      execution: ChatExecutionInfo | null | undefined,
      executionId: string,
      sessionId: string
    ) {
      return Boolean(
        execution &&
        execution.id === executionId &&
        execution.session_id === sessionId
      );
    },

    _setExecutionScope(executionId: string, cursorKnown = false) {
      const normalizedExecutionId = executionId.trim();
      const scopeChanged = normalizedExecutionId !== this.executionId ||
        normalizedExecutionId !== this.eventCursorExecutionId;
      if (scopeChanged) this._invalidateActiveStream();
      this.executionId = normalizedExecutionId;
      if (!normalizedExecutionId || scopeChanged) {
        this.eventCursorExecutionId = normalizedExecutionId;
        this.lastEventSequence = 0;
        this.processedSseEventIds = [];
        this.processedSseEventCharacterCount = 0;
        this.cursorKnown = Boolean(normalizedExecutionId && cursorKnown);
        this.activeAttemptId = '';
        return;
      }
      if (cursorKnown) this.cursorKnown = true;
    },

    _confirmedCursorForExecution(executionId: string) {
      return this.cursorKnown && this.eventCursorExecutionId === executionId
        ? this.lastEventSequence
        : 0;
    },

    _rememberSseFrameFingerprint(frameFingerprint: string) {
      this.processedSseEventIds.push(frameFingerprint);
      this.processedSseEventCharacterCount += frameFingerprint.length;
      while (
        this.processedSseEventIds.length > SSE_DEDUPE_MAX_ENTRIES ||
        this.processedSseEventCharacterCount > SSE_DEDUPE_MAX_CHARACTERS
      ) {
        const evicted = this.processedSseEventIds.shift();
        if (evicted === undefined) break;
        this.processedSseEventCharacterCount -= evicted.length;
      }
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
        if (!this._isConversationContextCurrent(contextToken)) return;

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

    async refreshAgents(preferredAgentId?: string): Promise<boolean> {
      const generation = this.agentRefreshGeneration + 1;
      this.agentRefreshGeneration = generation;
      this.agentRefreshController?.abort();
      const controller = markRaw(new AbortController());
      this.agentRefreshController = controller;

      const agentsById = new Map<string, AgentProfile>();
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      let rawItemCount = 0;

      try {
        for (let pageNumber = 0; pageNumber < AGENT_CATALOG_MAX_PAGES; pageNumber += 1) {
          const page = await api.agents(
            { cursor, limit: AGENT_PAGE_LIMIT },
            controller.signal
          );
          if (controller.signal.aborted || generation !== this.agentRefreshGeneration) {
            return false;
          }

          rawItemCount += page.items.length;
          if (rawItemCount > AGENT_CATALOG_MAX_ITEMS) {
            throw new Error('Agent catalog exceeds the safe item limit.');
          }
          for (const agent of page.items) {
            if (!agentsById.has(agent.id)) agentsById.set(agent.id, agent);
          }

          const nextCursor = page.next_cursor?.trim() || '';
          if (!nextCursor) {
            const agents = [...agentsById.values()];
            const currentStillExists = agents.some((agent) => agent.id === this.agentId);
            const nextAgentId = preferredAgentId && agents.some(
              (agent) => agent.id === preferredAgentId
            )
              ? preferredAgentId
              : currentStillExists
                ? this.agentId
                : agents[0]?.id ?? '';
            if (controller.signal.aborted || generation !== this.agentRefreshGeneration) {
              return false;
            }
            this.$patch({ agents, agentId: nextAgentId });
            return true;
          }
          if (seenCursors.has(nextCursor)) {
            throw new Error('Agent pagination returned a repeated cursor.');
          }
          seenCursors.add(nextCursor);
          cursor = nextCursor;
        }
        throw new Error('Agent catalog exceeds the safe page limit.');
      } catch (error) {
        if (controller.signal.aborted || generation !== this.agentRefreshGeneration) {
          return false;
        }
        throw error;
      } finally {
        if (generation === this.agentRefreshGeneration) {
          this.agentRefreshController = null;
        }
      }
    },

    async createAgent(payload: AgentProfilePayload) {
      const agent = await api.createAgent(payload);
      if (!(await this.refreshAgents(agent.id))) {
        throw new AgentCatalogRefreshCancelledError();
      }
      await this.chooseAgent(agent.id);
      return agent;
    },

    async updateAgent(agentId: string, payload: AgentProfileUpdatePayload) {
      const agent = await api.updateAgent(agentId, payload);
      if (!(await this.refreshAgents())) throw new AgentCatalogRefreshCancelledError();
      await this.chooseAgent(agent.id);
      return agent;
    },

    async createAgentVersion(agentId: string, payload: AgentVersionPayload) {
      await api.createAgentVersion(agentId, payload);
      if (!(await this.refreshAgents(agentId))) {
        throw new AgentCatalogRefreshCancelledError();
      }
      return api.agent(agentId);
    },

    async deleteAgent(agentId: string) {
      const deletingCurrent = this.agentId === agentId;
      await api.deleteAgent(agentId);
      if (!(await this.refreshAgents(deletingCurrent ? undefined : this.agentId))) {
        throw new AgentCatalogRefreshCancelledError();
      }
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
      const actionEpoch = this._beginRunAction();
      let requestExecutionId = this.executionId;
      const isActionCurrent = () => this._isRunActionCurrent(
        actionEpoch,
        contextToken,
        requestSessionId,
        requestExecutionId
      );
      let sessionInfo = this.sessionInfo;
      if (!sessionInfo || sessionInfo.session_id !== requestSessionId) {
        try {
          const fetchedSession = await api.session(requestSessionId);
          if (!isActionCurrent()) return false;
          if (fetchedSession.session_id !== requestSessionId) {
            throw new Error('Session response does not match the submit request.');
          }
          sessionInfo = fetchedSession;
          this.sessionInfo = fetchedSession;
        } catch (error) {
          if (!isActionCurrent()) return false;
          this.error = normalizeErrorMessage(error);
          return false;
        }
      }
      if (!isActionCurrent()) return false;
      if (!sessionInfo || sessionInfo.session_id !== requestSessionId) {
        this.error = '会话响应与当前发送请求不匹配。';
        return false;
      }
      if (sessionInfo.agent_id !== this.agentId) {
        this.error = '当前会话与所选 Agent 不一致，请重新选择 Agent 或新建会话。';
        return false;
      }
      this._invalidateActiveStream();
      const requestIdempotencyKey = this._nextIdempotencyKey();
      this.error = '';
      this.lastErrorCode = '';
      this.statusNotice = '';
      this.status = 'pending';
      this.runLifecycle = 'queued';
      requestExecutionId = '';
      this._setExecutionScope('');
      const optimisticUserMessage: WorkbenchMessage = {
        role: 'user',
        content: message,
        message_type: 'text',
        created_at: new Date().toISOString(),
        assistant_state: 'normal'
      };
      this.messages.push(optimisticUserMessage);
      this._ensureAssistantPlaceholder();
      try {
        const submitted = await api.sendMessage(requestSessionId, {
          message,
          idempotency_key: requestIdempotencyKey
        });
        if (!isActionCurrent()) return false;
        if (
          submitted.session_id !== requestSessionId ||
          !isCanonicalNonEmptyString(submitted.message_id) ||
          !isCanonicalNonEmptyString(submitted.execution_id)
        ) {
          throw new Error('Submit response does not match the requested session or execution.');
        }
        this.statusNotice = '';
        optimisticUserMessage.id = submitted.message_id;
        requestExecutionId = submitted.execution_id;
        this._setExecutionScope(submitted.execution_id, true);
        if (!isActionCurrent()) return false;
        this.listen(
          requestSessionId,
          contextToken,
          api.executionEvents(
            submitted.execution_id,
            this._confirmedCursorForExecution(submitted.execution_id)
          )
        );
        await this.refreshSessions(isActionCurrent).catch(() => undefined);
        if (!isActionCurrent()) return false;
        return true;
      } catch (error) {
        if (!isActionCurrent()) return false;
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
          if (!isActionCurrent()) return false;
        }
        return false;
      }
    },

    async resume(decision: ResumeDecision) {
      if (!this.canResume) return false;
      const contextToken = this.sessionContextToken;
      const requestSessionId = this.sessionId;
      const requestExecutionId = this.executionId;
      const interruptId = this.executionInfo?.interrupt?.interrupt_id;
      if (!requestExecutionId || !interruptId) return false;
      const actionEpoch = this._beginRunAction();
      const isActionCurrent = () => this._isRunActionCurrent(
        actionEpoch,
        contextToken,
        requestSessionId,
        requestExecutionId
      );
      this._invalidateActiveStream();
      this.error = '';
      this.lastErrorCode = '';
      this.statusNotice = '';
      this.status = 'pending';
      this.runLifecycle = 'queued';
      this._ensureAssistantPlaceholder();
      try {
        if (this.sessionInfo?.agent_id !== this.agentId) {
          throw new Error('当前会话与所选 Agent 不一致，请重新选择 Agent 后继续。');
        }
        const execution = await api.resumeRun(requestExecutionId, {
          interrupt_id: interruptId,
          decision
        });
        if (!isActionCurrent()) return false;
        if (!this._isExpectedExecutionResponse(execution, requestExecutionId, requestSessionId)) {
          throw new Error('Resume response does not match the requested execution.');
        }
        this._setExecutionScope(execution.id);
        this.executionInfo = execution;
        const resumeCursorKnown = this.cursorKnown &&
          this.eventCursorExecutionId === execution.id;
        if (resumeCursorKnown) {
          this.listen(
            requestSessionId,
            contextToken,
            api.executionEvents(
              execution.id,
              this._confirmedCursorForExecution(execution.id)
            )
          );
        } else {
          this._setRunLifecycle('reconnecting');
          this.statusNotice = '正在刷新恢复后的任务状态…';
          this._startRunStatusPolling(requestSessionId, contextToken);
        }
        await this.refreshSessions(isActionCurrent).catch(() => undefined);
        if (!isActionCurrent()) return false;
        return true;
      } catch (error) {
        if (!isActionCurrent()) return false;
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        this.error = normalizeErrorMessage(error);
        this.status = 'failed';
        this.runLifecycle = 'failed';
        this._removeActiveStreamingAssistantPlaceholder();
        await this.refreshSession(
          false,
          isActionCurrent,
          undefined,
          requestExecutionId
        ).catch(() => undefined);
        if (!isActionCurrent()) return false;
        if (error instanceof ApiError && error.code === 'SESSION_AGENT_MISMATCH') {
          await this.chooseAgent(this.agentId, true);
          if (!isActionCurrent()) return false;
        }
        return false;
      }
    },

    async cancelActiveRun() {
      if (!['queued', 'running', 'reconnecting'].includes(this.runLifecycle) || !this.sessionId) return;
      const requestExecutionId = this.executionId;
      if (!requestExecutionId) {
        this.statusNotice = '任务正在提交，获得执行 ID 后即可取消。';
        return;
      }
      const contextToken = this.sessionContextToken;
      const sessionId = this.sessionId;
      const actionEpoch = this._beginRunAction();
      const isActionCurrent = () => this._isRunActionCurrent(
        actionEpoch,
        contextToken,
        sessionId,
        requestExecutionId
      );
      this._setRunLifecycle('cancelling');
      this.statusNotice = '正在取消生成…';
      this._invalidateActiveStream();

      try {
        const execution = await api.cancelRun(requestExecutionId);
        if (!isActionCurrent()) return;
        if (!this._isExpectedExecutionResponse(execution, requestExecutionId, sessionId)) {
          throw new Error('Cancel response does not match the requested execution.');
        }
        this._setExecutionScope(execution.id);
        this.executionInfo = execution;

        for (let attempt = 0; attempt < 20; attempt += 1) {
          const latest = await api.session(sessionId);
          if (!isActionCurrent()) return;
          const latestExecution = latest.latest_execution;
          if (
            latest.session_id !== sessionId ||
            !latestExecution ||
            !this._isExpectedExecutionResponse(
              latestExecution,
              requestExecutionId,
              sessionId
            )
          ) {
            throw new Error('Cancel polling response does not match the requested execution.');
          }
          this.sessionInfo = latest;
          this.executionInfo = latestExecution;
          this._setExecutionScope(latestExecution.id);
          const lifecycle = toRunLifecycle(latestExecution.status);
          if (!['queued', 'running', 'reconnecting'].includes(lifecycle)) {
            this._setRunLifecycle(lifecycle);
            this.statusNotice = lifecycle === 'cancelled' ? '已停止生成。' : '';
            this._preserveStreamingAssistantContent();
            await this.refreshSessions(isActionCurrent).catch(() => undefined);
            if (!isActionCurrent()) return;
            return;
          }
          await new Promise<void>((resolve) => window.setTimeout(resolve, 250));
          if (!isActionCurrent()) return;
        }
        if (!isActionCurrent()) return;
        this.statusNotice = '取消请求已发送，正在等待任务停止。';
      } catch (error) {
        if (!isActionCurrent()) return;
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        this.error = normalizeErrorMessage(error);
        this.statusNotice = '';
        this._setRunLifecycle('running');
      }
    },

    listen(sessionId: string, contextToken?: number, sourceOverride?: FetchEventStream) {
      const resolvedContextToken = contextToken ?? this.sessionContextToken;
      if (!this._isConversationContextCurrent(resolvedContextToken)) {
        sourceOverride?.close();
        return;
      }
      const streamExecutionId = this.executionId.trim();
      if (sourceOverride && !streamExecutionId) {
        sourceOverride.close();
        return;
      }
      if (sourceOverride) this._setExecutionScope(streamExecutionId);
      const generation = this._invalidateActiveStream();
      this.activeSessionId = sessionId;
      const isActiveContext = () =>
        this._isStreamGenerationCurrent(generation) &&
        this._isConversationContextCurrent(resolvedContextToken) &&
        this.activeSessionId === sessionId &&
        (!streamExecutionId || this.executionId === streamExecutionId);

      if (!sourceOverride) {
        this._scheduleSseRecovery(sessionId, resolvedContextToken, generation);
        return;
      }
      const source = sourceOverride;
      this.eventSource = source;
      let terminalEventReceived = false;

      const closeOwnSource = () => {
        source.close();
        if (isActiveContext()) this.eventSource = null;
      };

      const degradeStream = () => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        closeOwnSource();
        this._setRunLifecycle('reconnecting');
        this.statusNotice = '实时事件校验失败，正在刷新任务状态…';
        this._startRunStatusPolling(
          sessionId,
          resolvedContextToken,
          generation
        );
      };

      const consumeFrame = (event: MessageEvent) => {
        if (terminalEventReceived || !isActiveContext()) return null;
        const result = this._consumeSseFrame(event, streamExecutionId);
        if (result.kind === 'invalid') {
          degradeStream();
          return null;
        }
        if (result.kind === 'duplicate') return null;
        return result;
      };

      const recoverTerminalReconcile = (lifecycle: TerminalRunLifecycle) => {
        if (!isActiveContext()) return;
        closeOwnSource();
        this.statusNotice = '正在重新同步任务状态…';
        this._startRunStatusPolling(
          sessionId,
          resolvedContextToken,
          generation,
          lifecycle
        );
      };

      const reconcileTerminal = async (
        hydrateMessages: boolean,
        lifecycle: TerminalRunLifecycle
      ) => {
        try {
          const refreshed = await this.refreshSession(
            hydrateMessages,
            isActiveContext,
            lifecycle,
            streamExecutionId
          );
          if (!isActiveContext()) return false;
          if (!refreshed) {
            recoverTerminalReconcile(lifecycle);
            return false;
          }
          await this.refreshSessions(isActiveContext);
          if (!isActiveContext()) return false;
          return true;
        } catch {
          if (isActiveContext()) recoverTerminalReconcile(lifecycle);
          return false;
        }
      };

      const completeRun = async () => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('completed');
        if (!(await reconcileTerminal(true, 'completed'))) return;
        this._removeActiveStreamingAssistantPlaceholder();
        closeOwnSource();
      };

      const cancelRun = async () => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('cancelled');
        this._preserveStreamingAssistantContent();
        if (!(await reconcileTerminal(false, 'cancelled'))) return;
        closeOwnSource();
      };

      const waitForInput = async (name: string, data: Record<string, unknown>) => {
        if (terminalEventReceived || !isActiveContext()) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        this._setRunLifecycle('waiting_input');
        this._recordTimelineEvent(name || 'waiting_input', data);
        if (!(await reconcileTerminal(true, 'waiting_input'))) return;
        this._removeActiveStreamingAssistantPlaceholder();
        closeOwnSource();
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
        if (!(await reconcileTerminal(true, 'failed'))) return;
        this._removeActiveStreamingAssistantPlaceholder();
        closeOwnSource();
      };

      const handleMessageEvent = async (event: MessageEvent) => {
        const frame = consumeFrame(event);
        if (!frame || frame.kind !== 'accepted') return;
        const data = frame.data;
        if (this._isStaleAttemptPayload(data)) return;
        this._adoptAttemptFromPayload(data);
        const name = typeof data.name === 'string' ? data.name : '';
        const content = typeof data.content === 'string' ? data.content : '';
        const chunk = typeof data.chunk === 'string' ? data.chunk : '';
        const messageType = (
          typeof data.message_type === 'string' &&
          (data.message_type === 'text' ||
            data.message_type === 'markdown' ||
            data.message_type === 'json')
        ) ? data.message_type : undefined;
        const messageId = typeof data.message_id === 'string' ? data.message_id.trim() : '';
        const createdAt = typeof data.timestamp === 'string' ? data.timestamp : '';
        const done = Boolean(data.done);
        this.statusNotice = '';
        this._setRunLifecycle('running');

        if (name === 'assistant_message_delta') {
          this._startStreamingAssistantMessage();
          this._appendAssistantMessageDelta(content || chunk, done, messageType || 'markdown');
          return;
        }

        if (name === 'assistant_message') {
          if (content) {
            // This event is the persisted final snapshot. Replacing the
            // streamed buffer avoids appending the full text a second time.
            this._appendAssistantMessage(
              content,
              messageType || 'text',
              messageId,
              createdAt
            );
          }
          return;
        }

        this._recordTimelineEvent(name || 'messages', data);
      };

      const handleLifecycleEvent = async (event: MessageEvent) => {
        const frame = consumeFrame(event);
        if (!frame || frame.kind !== 'accepted') return;
        const data = frame.data;
        const name = typeof data.name === 'string' ? data.name : '';

        if (name === 'run_retry') {
          // A recovered worker starts a fresh model attempt. Discard any
          // uncommitted partial text left by the crashed attempt so the new
          // stream cannot be concatenated with stale output.
          this._removeActiveStreamingAssistantPlaceholder();
          this._adoptAttemptFromPayload(data, true);
        } else if (name === 'attempt_start' || name === 'run_resume') {
          const attemptId = this._attemptIdFromPayload(data);
          if (attemptId && attemptId !== this.activeAttemptId) {
            if (this.activeAttemptId || this.assistantStreamingBuffer) {
              this._removeActiveStreamingAssistantPlaceholder();
            }
            this.activeAttemptId = attemptId;
          }
        } else if (name === 'run_start') {
          this._adoptAttemptFromPayload(data, true);
        } else if (this._isStaleAttemptPayload(data)) {
          return;
        }

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
        const frame = consumeFrame(event);
        if (!frame || (frame.kind !== 'accepted' && frame.kind !== 'control')) return;
        const data = frame.data;
        if (this._isStaleAttemptPayload(data)) return;
        this._adoptAttemptFromPayload(data);
        const code = typeof data.code === 'string'
          ? data.code
          : typeof data.error_code === 'string'
            ? data.error_code
            : '';
        if (frame.kind === 'control' || STREAM_DEGRADED_CODES.has(code)) {
          degradeStream();
          return;
        }
        await failRun(data);
      };

      const handleToolEvent = (event: MessageEvent) => {
        const frame = consumeFrame(event);
        if (!frame || frame.kind !== 'accepted') return;
        const data = frame.data;
        if (this._isStaleAttemptPayload(data)) return;
        this._adoptAttemptFromPayload(data);
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
        const frame = consumeFrame(messageEvent);
        if (!frame || frame.kind !== 'accepted') return;
        const data = frame.data;
        if (this._isStaleAttemptPayload(data)) return;
        this._adoptAttemptFromPayload(data);
        this._projectInterruptFromPayload(data, streamExecutionId, sessionId);
        const name = typeof data.name === 'string' ? data.name : '';
        void waitForInput(name, data);
      });

      source.addEventListener('errors', (event) => {
        void handleErrorEvent(event as MessageEvent);
      });

      source.addEventListener('values', (event) => {
        const messageEvent = event as MessageEvent;
        const frame = consumeFrame(messageEvent);
        if (!frame || frame.kind !== 'accepted') return;
        const data = frame.data;
        if (this._isStaleAttemptPayload(data)) return;
        this._adoptAttemptFromPayload(data);
        const name = typeof data.name === 'string' ? data.name : 'values';
        this._recordTimelineEvent(name, data);
      });

      source.addEventListener('close', async () => {
        if (!isActiveContext()) return;
        try {
          if (!terminalEventReceived) {
            terminalEventReceived = true;
            await this._recoverFromSseRunState(
              sessionId,
              true,
              generation
            );
          }
        } catch {
          if (isActiveContext()) {
            this._startRunStatusPolling(
              sessionId,
              resolvedContextToken,
              generation
            );
          }
        } finally {
          closeOwnSource();
        }
      });

      source.onerror = async (error) => {
        if (!isActiveContext() || terminalEventReceived) return;
        terminalEventReceived = true;
        closeOwnSource();
        this._setRunLifecycle('reconnecting');
        this.statusNotice = '实时连接中断，正在恢复运行状态…';
        this.lastErrorCode = error instanceof ApiError ? error.code : '';
        try {
          await this._recoverFromSseRunState(sessionId, true, generation);
        } catch {
          if (isActiveContext()) {
            this._startRunStatusPolling(
              sessionId,
              resolvedContextToken,
              generation
            );
          }
        }
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

    _attemptIdFromPayload(data: Record<string, unknown>) {
      return typeof data.attempt_id === 'string' ? data.attempt_id.trim() : '';
    },

    _isStaleAttemptPayload(data: Record<string, unknown>) {
      const attemptId = this._attemptIdFromPayload(data);
      return Boolean(attemptId && this.activeAttemptId && attemptId !== this.activeAttemptId);
    },

    _adoptAttemptFromPayload(data: Record<string, unknown>, replace = false) {
      const attemptId = this._attemptIdFromPayload(data);
      if (attemptId && (replace || !this.activeAttemptId)) {
        this.activeAttemptId = attemptId;
      }
    },

    _projectInterruptFromPayload(
      data: Record<string, unknown>,
      executionId: string,
      sessionId: string
    ) {
      if (
        this.executionId !== executionId ||
        this.sessionId !== sessionId ||
        !isPublicInterrupt(data.interrupt)
      ) return;
      const existingExecution = this.executionInfo;
      const currentExecution = existingExecution?.id === executionId &&
        existingExecution.session_id === sessionId
        ? existingExecution
        : null;
      const interrupt: PublicInterrupt = {
        interrupt_id: data.interrupt.interrupt_id,
        actions: data.interrupt.actions.map((action) => ({
          tool_name: action.tool_name,
          purpose: action.purpose,
          ...(action.memory === undefined
            ? {}
            : {
                memory: action.memory === null
                  ? null
                  : {
                      type: action.memory.type,
                      content: action.memory.content
                    }
              })
        }))
      };
      this.executionInfo = {
        ...(currentExecution ?? {}),
        id: executionId,
        session_id: sessionId,
        status: 'waiting_input',
        error: currentExecution?.error ?? null,
        interrupt
      };
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

    _consumeSseFrame(event: MessageEvent, streamExecutionId: string): SseFrameResult {
      if (
        typeof event.data !== 'string' ||
        typeof event.lastEventId !== 'string' ||
        !SSE_CHANNELS.has(event.type)
      ) return { kind: 'invalid' };

      let envelope: Record<string, unknown>;
      try {
        const parsed = JSON.parse(event.data) as unknown;
        if (!isRecord(parsed)) return { kind: 'invalid' };
        envelope = parsed;
      } catch {
        return { kind: 'invalid' };
      }

      if (
        envelope.schema_version !== 3 ||
        envelope.channel !== event.type ||
        envelope.execution_id !== streamExecutionId ||
        this.executionId !== streamExecutionId ||
        !isRecord(envelope.data)
      ) return { kind: 'invalid' };

      const data: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(envelope.data)) {
        if (!SSE_RESERVED_DATA_KEYS.has(key)) data[key] = value;
      }
      data.schema_version = 3;
      data.execution_id = streamExecutionId;
      data.channel = event.type;
      data.event_type = event.type;

      const hasSequence = Object.prototype.hasOwnProperty.call(envelope, 'sequence');
      const hasEventId = Object.prototype.hasOwnProperty.call(envelope, 'event_id');
      if (!hasSequence && !hasEventId) {
        if (
          event.type !== 'errors' ||
          event.lastEventId !== '' ||
          data.name !== 'stream_exception' ||
          typeof data.code !== 'string' ||
          !data.code.trim() ||
          typeof data.message !== 'string' ||
          !data.message.trim()
        ) return { kind: 'invalid' };
        return { kind: 'control', data };
      }
      if (!hasSequence || !hasEventId) return { kind: 'invalid' };

      const sequence = envelope.sequence;
      if (typeof sequence !== 'number' || !Number.isSafeInteger(sequence) || sequence < 1) {
        return { kind: 'invalid' };
      }
      const expectedEventId = `${streamExecutionId}:${sequence}`;
      if (envelope.event_id !== expectedEventId || event.lastEventId !== expectedEventId) {
        return { kind: 'invalid' };
      }
      if (
        !Array.isArray(envelope.namespace) ||
        !envelope.namespace.every((item) => typeof item === 'string') ||
        !Object.prototype.hasOwnProperty.call(envelope, 'attempt_id') ||
        !Object.prototype.hasOwnProperty.call(envelope, 'message_id') ||
        !Object.prototype.hasOwnProperty.call(envelope, 'tool_call_id') ||
        !isNullableString(envelope.attempt_id) ||
        !isNullableString(envelope.message_id) ||
        !isNullableString(envelope.tool_call_id) ||
        !isTimezoneAwareIsoTimestamp(envelope.timestamp)
      ) return { kind: 'invalid' };

      data.sequence = sequence;
      data.event_id = expectedEventId;
      data.namespace = [...envelope.namespace];
      data.attempt_id = envelope.attempt_id;
      data.message_id = envelope.message_id;
      data.tool_call_id = envelope.tool_call_id;
      data.timestamp = envelope.timestamp;
      if (!hasValidSemanticSseData(event.type, data, true)) {
        return { kind: 'invalid' };
      }
      if (
        this.eventCursorExecutionId !== streamExecutionId ||
        !Number.isSafeInteger(this.lastEventSequence) ||
        this.lastEventSequence < 0 ||
        (!this.cursorKnown && this.lastEventSequence !== 0)
      ) return { kind: 'invalid' };

      const frameFingerprint = `${event.type}\u0000${event.lastEventId}\u0000${event.data}`;
      if (sequence <= this.lastEventSequence) {
        return this.processedSseEventIds.includes(frameFingerprint)
          ? { kind: 'duplicate' }
          : { kind: 'invalid' };
      }
      if (sequence !== this.lastEventSequence + 1) return { kind: 'invalid' };

      this._rememberSseFrameFingerprint(frameFingerprint);
      this.lastEventSequence = sequence;
      this.cursorKnown = true;
      return { kind: 'accepted', data };
    },

    async refreshSession(
      hydrateMessages = true,
      guard?: () => boolean,
      expectedLifecycle?: TerminalRunLifecycle,
      expectedExecutionId?: string
    ): Promise<boolean> {
      if (!this.sessionId || (guard && !guard())) return false;
      const contextToken = this.sessionContextToken;
      const requestedSessionId = this.sessionId;
      const sessionInfo = await this._loadSessionHistory(requestedSessionId, contextToken);
      if (
        !sessionInfo ||
        !this._isConversationContextCurrent(contextToken) ||
        this.sessionId !== requestedSessionId ||
        (guard && !guard())
      ) return false;
      if (sessionInfo.session_id !== requestedSessionId) return false;
      const latestExecution = sessionInfo.latest_execution;
      if (
        expectedExecutionId !== undefined &&
        (
          !latestExecution ||
          latestExecution.id !== expectedExecutionId ||
          latestExecution.session_id !== requestedSessionId
        )
      ) return false;
      if (expectedLifecycle && expectedExecutionId === undefined) return false;
      const latestStatus = resolveSessionStatus(sessionInfo, 'idle');
      const latestLifecycle = toRunLifecycle(latestStatus);
      if (expectedLifecycle && latestLifecycle !== expectedLifecycle) return false;
      this.sessionInfo = sessionInfo;
      this.executionInfo = this.sessionInfo.latest_execution;
      this._setExecutionScope(this.executionInfo?.id ?? '');
      this.status = latestStatus;
      this._setRunLifecycle(latestLifecycle);
      this.statusNotice = '';
      if (hydrateMessages) {
        this._hydrateMessages(this.sessionInfo, true);
      }
      if (!['queued', 'running', 'reconnecting'].includes(this.runLifecycle)) {
        this._removeActiveStreamingAssistantPlaceholder();
        return true;
      }
      if (['queued', 'running', 'reconnecting'].includes(this.runLifecycle)) {
        this._ensureAssistantPlaceholder();
      }
      return true;
    },

    _appendAssistantMessage(
      content: string,
      messageType: WorkbenchMessage['message_type'] = 'text',
      messageId = '',
      createdAt = ''
    ) {
      if (!content && content !== '') return;
      const applyFinalMetadata = (message: WorkbenchMessage) => {
        if (messageId) message.id = messageId;
        if (createdAt) message.created_at = createdAt;
      };
      if (messageId) {
        const persisted = this.messages.find((message) => message.id === messageId);
        if (persisted?.role === 'assistant') {
          persisted.content = content;
          persisted.message_type = messageType;
          persisted.assistant_state = 'normal';
          applyFinalMetadata(persisted);
          this.assistantStreamingBuffer = content;
          this._clearStreamingAssistantMessage();
          this._setRunLifecycle('running');
          return;
        }
      }
      const streamingIndex = this._findActiveAssistantPlaceholderIndex();
      if (streamingIndex >= 0) {
        const target = this.messages[streamingIndex];
        if (target && target.role === 'assistant') {
          target.content = content;
          target.message_type = messageType;
          target.assistant_state = 'normal';
          applyFinalMetadata(target);
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
          applyFinalMetadata(target);
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
          applyFinalMetadata(latest);
          this._clearStreamingAssistantMessage();
          this.assistantStreamingBuffer = content;
          return;
        }
      }
      const assistant: WorkbenchMessage = {
        role: 'assistant',
        content,
        message_type: messageType,
        assistant_state: 'normal'
      };
      applyFinalMetadata(assistant);
      this.messages.push(assistant);
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

    async _recoverFromSseRunState(
      sessionId: string,
      connectionError = false,
      expectedGeneration?: number
    ) {
      const resolvedGeneration = expectedGeneration ?? this.streamGeneration;
      const contextToken = this.sessionContextToken;
      const requestedExecutionId = this.executionId;
      const isCurrent = () =>
        this._isStreamGenerationCurrent(resolvedGeneration) &&
        this._isConversationContextCurrent(contextToken) &&
        this.sessionId === sessionId &&
        this.executionId === requestedExecutionId;
      if (!requestedExecutionId || !isCurrent()) return;
      const execution = await api.runStatus(requestedExecutionId).catch(() => null);
      if (!isCurrent()) return;
      if (execution) {
        if (!this._isExpectedExecutionResponse(execution, requestedExecutionId, sessionId)) {
          this.statusNotice = '任务状态响应不匹配，正在重试…';
          this._startRunStatusPolling(sessionId, contextToken, resolvedGeneration);
          return;
        }
        this.executionInfo = execution;
        this._setExecutionScope(execution.id);
      }
      const status = execution?.status ?? this.executionInfo?.status;
      const runLifecycle = toRunLifecycle(status);
      if (runLifecycle === 'queued' || runLifecycle === 'running') {
        if (execution?.streaming_degraded) {
          this._setRunLifecycle('reconnecting');
          this.statusNotice = '实时更新已降级，正在每 3 秒刷新任务状态…';
          this._startRunStatusPolling(sessionId, contextToken, resolvedGeneration);
          return;
        }
        if (connectionError) {
          this._setRunLifecycle('reconnecting');
          this.statusNotice = '实时连接中断，正在从上次位置恢复…';
          this._scheduleSseRecovery(sessionId, contextToken, resolvedGeneration);
        } else {
          this._setRunLifecycle(runLifecycle);
        }
        return;
      }
      this._stopSseRecoveryPoll();
      this._setRunLifecycle(runLifecycle);
      try {
        const refreshed = await this.refreshSession(
          true,
          isCurrent,
          isTerminalRunLifecycle(runLifecycle) ? runLifecycle : undefined,
          requestedExecutionId
        );
        if (!isCurrent()) return;
        if (!refreshed) {
          this._startRunStatusPolling(
            sessionId,
            contextToken,
            resolvedGeneration,
            isTerminalRunLifecycle(runLifecycle) ? runLifecycle : undefined
          );
          return;
        }
      } catch {
        if (isCurrent()) {
          this._startRunStatusPolling(
            sessionId,
            contextToken,
            resolvedGeneration,
            isTerminalRunLifecycle(runLifecycle) ? runLifecycle : undefined
          );
        }
        return;
      }
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

    _hydrateMessages(session: ChatSessionDetail, preserveLocal = false) {
      const serverMessagesById = new Map<string, WorkbenchMessage>();
      for (const message of session.messages) {
        if (!['text', 'markdown'].includes(message.message_type)) continue;
        if (serverMessagesById.has(message.id)) continue;
        serverMessagesById.set(message.id, {
          id: message.id,
          role: message.role as WorkbenchMessage['role'],
          content: message.content,
          message_type: message.message_type,
          created_at: message.created_at,
          assistant_state: 'normal' as AssistantBubbleState
        });
      }
      const mapped = [...serverMessagesById.values()];
      if (!preserveLocal) {
        this.messages = mapped;
        return;
      }

      const representedSignatures = new Map<string, number>();
      for (const message of mapped) {
        const signature = messageSignature(message);
        representedSignatures.set(signature, (representedSignatures.get(signature) ?? 0) + 1);
      }
      const localOnly: WorkbenchMessage[] = [];
      for (const message of this.messages) {
        const signature = messageSignature(message);
        if (message.id && serverMessagesById.has(message.id)) {
          const remaining = representedSignatures.get(signature) ?? 0;
          if (remaining > 0) representedSignatures.set(signature, remaining - 1);
          continue;
        }
        if (message.id) {
          localOnly.push(message);
          continue;
        }
        const remaining = representedSignatures.get(signature) ?? 0;
        if (remaining > 0) {
          representedSignatures.set(signature, remaining - 1);
          continue;
        }
        localOnly.push(message);
      }
      this.messages = [...mapped, ...localOnly].sort((left, right) => {
        if (!left.created_at && !right.created_at) return 0;
        if (!left.created_at) return 1;
        if (!right.created_at) return -1;
        return compareDatedMessages(
          { id: left.id ?? '', created_at: left.created_at },
          { id: right.id ?? '', created_at: right.created_at }
        );
      });
    },

    dispose() {
      this.agentRefreshGeneration += 1;
      this.agentRefreshController?.abort();
      this.agentRefreshController = null;
      this.sessionContextToken += 1;
      this.resetConversationContext();
      this.isLoadingSession = false;
      this.isSwitchingAgent = false;
    },

    _stopSseRecoveryPoll(resetAttempts = true) {
      this.ssePollEpoch += 1;
      if (this.ssePollTimer) {
        window.clearTimeout(this.ssePollTimer);
        this.ssePollTimer = null;
      }
      if (resetAttempts) {
        this.sseRecoveryAttempts = 0;
      }
    },

    _scheduleSseRecovery(
      sessionId: string,
      contextToken?: number,
      expectedGeneration?: number
    ) {
      const resolvedGeneration = expectedGeneration ?? this.streamGeneration;
      const resolvedContextToken = contextToken ?? this.sessionContextToken;
      const pollEpoch = this.ssePollEpoch;
      const isCurrent = () =>
        this._isStreamGenerationCurrent(resolvedGeneration) &&
        this._isConversationContextCurrent(resolvedContextToken) &&
        this.sessionId === sessionId &&
        this.ssePollEpoch === pollEpoch;
      if (!isCurrent()) return;
      if (this.ssePollTimer) return;
      if (this.sseRecoveryAttempts >= SSE_RECOVERY_MAX_ATTEMPTS) {
        this.statusNotice = '实时连接持续失败，已切换为每 3 秒刷新任务状态…';
        this._startRunStatusPolling(sessionId, resolvedContextToken, resolvedGeneration);
        return;
      }
      const delay = Math.min(
        SSE_RECOVERY_INITIAL_DELAY_MS * 2 ** this.sseRecoveryAttempts,
        SSE_RECOVERY_MAX_DELAY_MS
      );
      const poll = async () => {
        if (!isCurrent()) return;
        const requestedExecutionId = this.executionId;
        if (requestedExecutionId) {
          const execution = await api.runStatus(requestedExecutionId).catch(() => null);
          if (!isCurrent() || this.executionId !== requestedExecutionId) return;
          if (execution) {
            if (!this._isExpectedExecutionResponse(execution, requestedExecutionId, sessionId)) {
              this.ssePollTimer = null;
              this.statusNotice = '任务状态响应不匹配，正在重试…';
              this._startRunStatusPolling(
                sessionId,
                resolvedContextToken,
                resolvedGeneration
              );
              return;
            }
            this.executionInfo = execution;
            this._setExecutionScope(execution.id);
            const lifecycle = toRunLifecycle(execution.status);
            if (!['queued', 'running'].includes(lifecycle)) {
              this.ssePollTimer = null;
              this._setRunLifecycle(lifecycle);
              try {
                const refreshed = await this.refreshSession(
                  true,
                  isCurrent,
                  isTerminalRunLifecycle(lifecycle) ? lifecycle : undefined,
                  requestedExecutionId
                );
                if (!isCurrent()) return;
                if (!refreshed) throw new Error('Session refresh was not applied.');
                await this.refreshSessions(isCurrent);
                if (!isCurrent()) return;
                this._removeActiveStreamingAssistantPlaceholder();
              } catch {
                if (isCurrent()) {
                  this._startRunStatusPolling(
                    sessionId,
                    resolvedContextToken,
                    resolvedGeneration,
                    isTerminalRunLifecycle(lifecycle) ? lifecycle : undefined
                  );
                }
              }
              return;
            }
            if (execution.streaming_degraded) {
              this.ssePollTimer = null;
              this._setRunLifecycle('reconnecting');
              this.statusNotice = '实时更新已降级，正在每 3 秒刷新任务状态…';
              this._startRunStatusPolling(
                sessionId,
                resolvedContextToken,
                resolvedGeneration
              );
              return;
            }
            this._setRunLifecycle(lifecycle);
            this.statusNotice = queueStageNotice(execution);
          }
        }
        this.ssePollTimer = null;
        if (
          isCurrent() &&
          ['queued', 'running', 'reconnecting'].includes(this.runLifecycle) && this.executionId
        ) {
          this.sseRecoveryAttempts += 1;
          this.listen(
            sessionId,
            resolvedContextToken,
            api.executionEvents(
              this.executionId,
              this._confirmedCursorForExecution(this.executionId)
            )
          );
        }
      };
      this.ssePollTimer = window.setTimeout(poll, delay);
    },

    _startRunStatusPolling(
      sessionId: string,
      contextToken?: number,
      expectedGeneration?: number,
      terminalLifecycle?: TerminalRunLifecycle
    ) {
      const resolvedGeneration = expectedGeneration ?? this.streamGeneration;
      const resolvedContextToken = contextToken ?? this.sessionContextToken;
      const isExpectedContext = () =>
        this._isStreamGenerationCurrent(resolvedGeneration) &&
        this._isConversationContextCurrent(resolvedContextToken) &&
        this.sessionId === sessionId;
      if (!isExpectedContext()) return;
      this._stopSseRecoveryPoll(false);
      const pollEpoch = this.ssePollEpoch;
      const isCurrent = () =>
        isExpectedContext() && this.ssePollEpoch === pollEpoch;
      let retryAttempts = 0;
      let reconciliationLifecycle = terminalLifecycle;
      const scheduleRetry = (poll: () => Promise<void>) => {
        if (!isCurrent()) return;
        if (reconciliationLifecycle) {
          retryAttempts += 1;
          if (retryAttempts >= SSE_RECOVERY_MAX_ATTEMPTS) {
            this.statusNotice = '任务状态同步持续失败，请稍后重试。';
            return;
          }
        }
        this.ssePollTimer = window.setTimeout(poll, RUN_STATUS_POLL_MS);
      };
      const poll = async () => {
        if (!isCurrent()) return;
        if (!this.executionId) {
          this.ssePollTimer = null;
          return;
        }
        const requestedExecutionId = this.executionId;
        const execution = await api.runStatus(requestedExecutionId).catch(() => null);
        if (
          !isCurrent() ||
          this.executionId !== requestedExecutionId
        ) {
          return;
        }
        this.ssePollTimer = null;
        if (
          !execution ||
          !this._isExpectedExecutionResponse(execution, requestedExecutionId, sessionId)
        ) {
          if (execution) this.statusNotice = '任务状态响应不匹配，正在重试…';
          scheduleRetry(poll);
          return;
        }
        const lifecycle = toRunLifecycle(execution.status);
        if (lifecycle === 'queued' || lifecycle === 'running') {
          if (reconciliationLifecycle) {
            this._setRunLifecycle(reconciliationLifecycle);
            this.statusNotice = '正在等待任务终态持久化…';
            scheduleRetry(poll);
            return;
          }
          this.executionInfo = execution;
          this._setExecutionScope(execution.id);
          this._setRunLifecycle(execution.streaming_degraded ? 'reconnecting' : lifecycle);
          this.statusNotice = execution.streaming_degraded
            ? '实时更新已降级，正在每 3 秒刷新任务状态…'
            : queueStageNotice(execution);
          scheduleRetry(poll);
          return;
        }
        if (!isTerminalRunLifecycle(lifecycle)) {
          scheduleRetry(poll);
          return;
        }
        this.executionInfo = execution;
        this._setExecutionScope(execution.id);
        reconciliationLifecycle = lifecycle;
        this._setRunLifecycle(lifecycle);
        this.statusNotice = lifecycle === 'cancelled' ? '已停止生成。' : '';
        try {
          const refreshed = await this.refreshSession(
            true,
            isCurrent,
            lifecycle,
            requestedExecutionId
          );
          if (!isCurrent()) return;
          if (!refreshed) throw new Error('Session refresh was not applied.');
          await this.refreshSessions(isCurrent);
          if (!isCurrent()) return;
          this._removeActiveStreamingAssistantPlaceholder();
        } catch {
          if (isCurrent()) scheduleRetry(poll);
        }
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
