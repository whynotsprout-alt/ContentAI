import { defineStore } from 'pinia';
import {
  api,
  type Account,
  type AccountPayload,
  type ChatSessionSummary,
  type AccountUpdatePayload,
  type ChatSessionDetail,
  type ChatExecutionInfo,
  type FetchEventStream
} from '../services/api';

const SSE_RECOVERY_INITIAL_DELAY_MS = 1000;
const SSE_RECOVERY_MAX_DELAY_MS = 10000;
const SSE_RECOVERY_MAX_ATTEMPTS = 8;

type AssistantBubbleState = 'normal' | 'pending' | 'streaming';
type RunLifecycle = 'idle' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';

export interface TimelineEvent {
  event: string;
  data: Record<string, unknown>;
}

type WorkbenchMessage = {
  role: 'user' | 'assistant' | 'system';
  content: string;
  message_type: 'text' | 'markdown' | 'json';
  assistant_state?: AssistantBubbleState;
};

function normalizeErrorMessage(value: unknown) {
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
  if (normalized === 'running') return 'running';
  if (normalized === 'completed') return 'completed';
  if (normalized === 'failed') return 'failed';
  if (normalized === 'cancelled' || normalized === 'canceled') return 'cancelled';
  if (normalized === 'interrupted') return 'interrupted';
  return 'idle';
}

function resolveSessionStatus(session: ChatSessionDetail, fallback: string = 'idle') {
  return typeof session.latest_execution?.status === 'string' && session.latest_execution.status
    ? session.latest_execution.status
    : session.latest_status ?? fallback;
}

export const useWorkbenchStore = defineStore('workbench', {
  state: () => ({
    accounts: [] as Account[],
    sessions: [] as ChatSessionSummary[],
    accountId: '',
    sessionId: '',
    executionId: '',
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
    processedSseEventIds: [] as string[],
    sessionContextToken: 0
  }),
  getters: {
    canSubmit(state) {
      return Boolean(
        state.sessionId &&
          !['running', 'interrupted'].includes(state.runLifecycle)
      );
    },
    canResume(state) {
      return Boolean(state.sessionId && state.status === 'interrupted');
    },
    selectedAccount(state) {
      return state.accounts.find((item) => item.id === state.accountId);
    },
    interruptPayload(state) {
      return state.executionInfo?.interrupt_payload ?? {};
    }
  },
  actions: {
    async boot() {
      await this.refreshAccounts();
      await this.refreshSessions();
      if (this.sessions.length) {
        await this.loadSession(this.sessions[0].session_id);
      } else {
        await this.startNewSession();
      }
    },

    async refreshSessions() {
      this.sessions = await api.sessions();
    },

    async startNewSession() {
      const contextToken = this._beginConversationContext();
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
      this.executionId = '';
      this.activeSessionId = '';

      if (!this.accountId) {
        this.error = '请先选择或创建一个账号后再新建会话。';
        return;
      }
      const session = await api.createSession({ account_id: this.accountId });
      if (!this._isConversationContextCurrent(contextToken)) return;
      this.sessionId = session.session_id;
      await this.refreshSessions();
    },

    async loadSession(sessionId: string) {
      if (!sessionId || sessionId === this.sessionId) return;
      const contextToken = this._beginConversationContext();
      this.eventSource?.close();
      this.eventSource = null;
      if (!this._isConversationContextCurrent(contextToken)) return;
      this._stopSseRecoveryPoll();
      if (!this._isConversationContextCurrent(contextToken)) return;
      this.processedSseEventIds = [];
      this.sessionInfo = null;
      this.executionInfo = null;

      const session = await api.session(sessionId);
      if (!this._isConversationContextCurrent(contextToken)) return;
      this.sessionId = session.session_id;
      this.messages = session.messages
        .filter((message) => ['text', 'markdown'].includes(message.message_type) && message.role !== 'tool')
        .map((message) => ({
          role: message.role as WorkbenchMessage['role'],
          content: message.content,
          message_type: message.message_type,
          assistant_state: 'normal'
        }));
      this.executionId = session.latest_execution?.id ?? '';
      this.activeSessionId = '';
      const resolvedStatus = resolveSessionStatus(session, 'idle');
      this.status = resolvedStatus;
      this.runLifecycle = toRunLifecycle(resolvedStatus);
      this.assistantStreamingState = 'normal';

      this.sessionInfo = session;
      this.executionInfo = session.latest_execution;
      if (this.runLifecycle === 'running') {
        this._scheduleSseRecovery(session.session_id, contextToken);
        this._ensureAssistantPlaceholder();
        return;
      }
      if (['failed', 'cancelled', 'interrupted'].includes(this.status)) {
        this._removeActiveStreamingAssistantPlaceholder();
      }
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
        if ((error as { status?: number }).status === 409) {
          this.error = '当前会话正在运行，完成后再删除。';
          return;
        }
        this.error = normalizeErrorMessage(error);
      }
    },

    async refreshAccounts(preferredAccountId?: string) {
      const accounts = await api.accounts();
      this.accounts = accounts;
      const currentStillExists = accounts.some((account) => account.id === this.accountId);
      if (preferredAccountId && accounts.some((account) => account.id === preferredAccountId)) {
        this.accountId = preferredAccountId;
      } else if (!currentStillExists) {
        this.accountId = accounts[0]?.id ?? '';
      }
    },

    async createAccount(payload: AccountPayload) {
      const account = await api.createAccount(payload);
      await this.refreshAccounts(account.id);
      return account;
    },

    async updateAccount(accountId: string, payload: AccountUpdatePayload) {
      const account = await api.updateAccount(accountId, payload);
      await this.refreshAccounts(account.id);
      return account;
    },

    async deleteAccount(accountId: string) {
      await api.deleteAccount(accountId);
      const nextAccount = this.accounts.find((account) => account.id !== accountId);
      await this.refreshAccounts(nextAccount?.id);
    },

    async submit(message: string) {
      if (!this.canSubmit) return;
      if (!this.accountId) {
        this.error = '请先选择或创建一个账号后再发送消息。';
        return;
      }
      const contextToken = this.sessionContextToken;
      const requestSessionId = this.sessionId;
      this.error = '';
      this.statusNotice = '';
      this.status = 'running';
      this.runLifecycle = 'running';
      this.processedSseEventIds = [];
      this.messages.push({ role: 'user', content: message, message_type: 'text' });
      this._ensureAssistantPlaceholder();
      try {
        const source = api.sendMessageStream(requestSessionId, {
          message
        });
        if (!this._isConversationContextCurrent(contextToken)) return;
        this.listen(requestSessionId, contextToken, source);
        await this.refreshSessions().catch(() => undefined);
      } catch (error) {
        if (!this._isConversationContextCurrent(contextToken)) return;
        this.error = normalizeErrorMessage(error);
        this.status = 'failed';
        this.runLifecycle = 'failed';
        this._removeActiveStreamingAssistantPlaceholder();
      }
    },

    async resume(message: string) {
      if (!this.canResume) return;
      const contextToken = this.sessionContextToken;
      const requestSessionId = this.sessionId;
      this.error = '';
      this.statusNotice = '';
      this.status = 'running';
      this.runLifecycle = 'running';
      this._ensureAssistantPlaceholder();
      this.processedSseEventIds = [];
      try {
        const source = api.resumeSessionStream(requestSessionId, { message });
        if (!this._isConversationContextCurrent(contextToken)) return;
        this.listen(requestSessionId, contextToken, source);
        await this.refreshSessions().catch(() => undefined);
      } catch (error) {
        if (!this._isConversationContextCurrent(contextToken)) return;
        this.error = normalizeErrorMessage(error);
        this.status = 'failed';
        this.runLifecycle = 'failed';
        this._removeActiveStreamingAssistantPlaceholder();
        await this.refreshSession(false).catch(() => undefined);
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

      const handleTokenEvent = async (event: MessageEvent) => {
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, 'token', event)) return;
        const data = this._parseSsePayload(event);
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
        const executionId = typeof data.execution_id === 'string' ? data.execution_id : '';
        this.statusNotice = '';
        this._setRunLifecycle('running');

        if (name === 'execution_started') {
          if (executionId) this.executionId = executionId;
          this._startStreamingAssistantMessage();
          this._recordTimelineEvent('execution_started', data);
          return;
        }

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

        if (name === 'execution_completed') {
          terminalEventReceived = true;
          this._stopSseRecoveryPoll();
          this._setRunLifecycle('completed');
          await this.refreshSession();
          await this.refreshSessions().catch(() => undefined);
          this._removeActiveStreamingAssistantPlaceholder();
          source.close();
          this.eventSource = null;
          return;
        }

        if (name === 'execution_failed') {
          terminalEventReceived = true;
          this._stopSseRecoveryPoll();
          this._setRunLifecycle('failed');
          if (!content) {
            this.error = normalizeErrorMessage('执行失败');
          } else if (content.includes(':')) {
            const [, reason] = content.split(':', 2);
            this.error = normalizeErrorMessage(reason?.trim() || '执行失败');
          } else {
            this.error = normalizeErrorMessage(content);
          }
          await this.refreshSession();
          await this.refreshSessions().catch(() => undefined);
          this._removeActiveStreamingAssistantPlaceholder();
          source.close();
          this.eventSource = null;
          return;
        }

        if (name === 'execution_cancelled') {
          terminalEventReceived = true;
          this._stopSseRecoveryPoll();
          this._setRunLifecycle('cancelled');
          await this.refreshSession();
          await this.refreshSessions().catch(() => undefined);
          this._removeActiveStreamingAssistantPlaceholder();
          source.close();
          this.eventSource = null;
          return;
        }

        if (name === 'execution_interrupted') {
          terminalEventReceived = true;
          this._stopSseRecoveryPoll();
          this._setRunLifecycle('interrupted');
          this._recordTimelineEvent('execution_interrupted', data);
          await this.refreshSession();
          await this.refreshSessions().catch(() => undefined);
          this._removeActiveStreamingAssistantPlaceholder();
          source.close();
          this.eventSource = null;
          return;
        }

        this._recordTimelineEvent(name || 'token', data);
      };

      source.addEventListener('token', (event) => {
        void handleTokenEvent(event as MessageEvent);
      });

      source.addEventListener('tool', (event) => {
        if (!isActiveContext()) return;
        if (this._hasSeenSseEvent(sessionId, 'tool', event as MessageEvent)) return;
        const data = this._parseSsePayload(event as MessageEvent);
        if (!data || !this._isActiveRunPayload(sessionId, data, resolvedContextToken)) return;
        this._recordTimelineEvent('tool', data);
      });

      source.addEventListener('close', async () => {
        if (!isActiveContext()) return;
        if (!terminalEventReceived) {
          terminalEventReceived = true;
          await this._recoverFromSseRunState(sessionId);
        }
        source.close();
        this.eventSource = null;
      });

      source.onerror = async () => {
        if (!isActiveContext() || terminalEventReceived) return;
        terminalEventReceived = true;
        source.close();
        this.eventSource = null;
        await this._recoverFromSseRunState(sessionId, true);
      };
    },

    _setRunLifecycle(lifecycle: RunLifecycle) {
      this.runLifecycle = lifecycle;
      if (lifecycle === 'running') {
        this.status = 'running';
      } else {
        this.status = lifecycle;
      }
    },

    _beginConversationContext() {
      this.sessionContextToken += 1;
      this.activeSessionId = '';
      this.status = 'idle';
      this.runLifecycle = 'idle';
      this.executionId = '';
      this.error = '';
      this.statusNotice = '';
      this.assistantStreamingState = 'normal';
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
      this.processedSseEventIds = [];
      this._stopSseRecoveryPoll();
      this._removeActiveStreamingAssistantPlaceholder();
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

    _hasSeenSseEvent(scopeId: string, eventName: string, event: MessageEvent) {
      const rawId = event.lastEventId || event.data;
      const key = `${scopeId}:${eventName}:${rawId}`;
      if (this.processedSseEventIds.includes(key)) {
        return true;
      }
      this.processedSseEventIds.push(key);
      this.processedSseEventIds = this.processedSseEventIds.slice(-400);
      return false;
    },

    _parseSsePayload(event: MessageEvent) {
      try {
        const data = JSON.parse(event.data) as unknown;
        return data && typeof data === 'object' ? (data as Record<string, unknown>) : null;
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

    async refreshSession(hydrateMessages = true) {
      if (!this.sessionId) return;
      const contextToken = this.sessionContextToken;
      this.sessionInfo = await api.session(this.sessionId);
      if (!this._isConversationContextCurrent(contextToken)) return;
      this.executionInfo = this.sessionInfo.latest_execution;
      this.executionId = this.executionInfo?.id ?? '';
      const latestStatus = resolveSessionStatus(this.sessionInfo, 'idle');
      this.status = latestStatus;
      this._setRunLifecycle(toRunLifecycle(latestStatus));
      this.statusNotice = '';
      if (hydrateMessages) {
        await this._hydrateMessages(this.sessionInfo);
      }
      if (this.runLifecycle !== 'running') {
        this._removeActiveStreamingAssistantPlaceholder();
        return;
      }
      if (this.runLifecycle === 'running') {
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
      await this.refreshSession().catch(() => undefined);
      const status = this.executionInfo?.status;
      const runLifecycle = toRunLifecycle(status);
      if (runLifecycle === 'running') {
        this._setRunLifecycle(runLifecycle);
        if (connectionError) {
          this._scheduleSseRecovery(sessionId, contextToken);
        }
        return;
      }
      this._stopSseRecoveryPoll();
      if (runLifecycle === 'failed') {
        this._setRunLifecycle('failed');
        this.error = normalizeErrorMessage(this.executionInfo?.error || '执行失败');
        return;
      }
      if (connectionError && !status) {
        this.error = '会话异常，请重试';
        this._setRunLifecycle('failed');
      }
      this._removeActiveStreamingAssistantPlaceholder();
    },

    async _hydrateMessages(session: ChatSessionDetail) {
      const mapped: WorkbenchMessage[] = session.messages
        .filter((message) => ['text', 'markdown'].includes(message.message_type) && message.role !== 'tool')
        .map((message) => ({
          role: message.role as WorkbenchMessage['role'],
          content: message.content,
          message_type: message.message_type,
          assistant_state: 'normal' as AssistantBubbleState
        }));
      this.messages = mapped;
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
        this.statusNotice = this.statusNotice || '连接恢复仍未成功，正在等待任务状态更新。';
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
        await this.refreshSession().catch(() => undefined);
        this.ssePollTimer = null;
        if (
          this._isConversationContextCurrent(resolvedContextToken) &&
          this.runLifecycle === 'running'
        ) {
          this.sseRecoveryAttempts += 1;
          this._scheduleSseRecovery(sessionId, resolvedContextToken);
        }
      };
      this.ssePollTimer = window.setTimeout(poll, delay);
    },

    _recordTimelineEvent(eventName: string, data: Record<string, unknown>) {
      this.events.push({ event: eventName, data });
      this.events = this.events.slice(-200);
    }
  }
});


