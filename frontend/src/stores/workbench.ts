import { defineStore } from 'pinia';
import {
  api,
  type Account,
  type AccountPayload,
  type ChatSessionSummary,
  type AccountUpdatePayload,
  type RunInfo
} from '../services/api';

export interface TimelineEvent {
  event: string;
  data: Record<string, unknown>;
}

type WorkbenchMessage = {
  role: 'user' | 'assistant' | 'system';
  content: string;
  message_type: 'text' | 'markdown' | 'json';
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

export const useWorkbenchStore = defineStore('workbench', {
  state: () => ({
    accounts: [] as Account[],
    sessions: [] as ChatSessionSummary[],
    accountId: '',
    sessionId: '',
    runId: '',
    activeRunId: '',
    status: 'idle',
    messages: [] as WorkbenchMessage[],
    events: [] as TimelineEvent[],
    runInfo: null as RunInfo | null,
    error: '',
    assistantStreamingBuffer: '',
    isStreamingAssistantMessage: false,
    streamingAssistantMessageIndex: -1,
    ssePollTimer: null as number | null,
    eventSource: null as EventSource | null,
    processedSseEventIds: [] as string[]
  }),
  getters: {
    canSubmit(state) {
      return Boolean(state.sessionId && !['running', 'queued'].includes(state.status));
    },
    selectedAccount(state) {
      return state.accounts.find((item) => item.id === state.accountId);
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
      this.eventSource?.close();
      this.eventSource = null;
      this._stopSseRecoveryPoll();
      this._clearStreamingAssistantMessage();
      this.error = '';
      this.status = 'idle';
      this.messages = [];
      this.events = [];
      this.runInfo = null;
      this.runId = '';
      this.activeRunId = '';

      const session = await api.createSession();
      this.sessionId = session.session_id;
      await this.refreshSessions();
    },

    async loadSession(sessionId: string) {
      if (!sessionId || sessionId === this.sessionId) return;
      this.eventSource?.close();
      this.eventSource = null;
      this._stopSseRecoveryPoll();
      this._clearStreamingAssistantMessage();
      this.processedSseEventIds = [];
      this.error = '';
      this.runInfo = null;

      const session = await api.session(sessionId);
      this.sessionId = session.session_id;
      this.messages = session.messages
        .filter((message) => ['text', 'markdown'].includes(message.message_type) && message.role !== 'tool')
        .map((message) => ({
          role: message.role as WorkbenchMessage['role'],
          content: message.content,
          message_type: message.message_type
        }));
      this.runId = session.latest_run_id ?? '';
      this.activeRunId = '';
      this.status = session.latest_status ?? 'idle';

      if (session.latest_run_id) {
        await this.refreshRun(session.latest_run_id, false);
        if (['queued', 'running'].includes(this.status)) {
          this.listen(session.latest_run_id);
        }
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
      this.error = '';
      this.status = 'running';
      this._clearStreamingAssistantMessage();
      this.processedSseEventIds = [];
      this.messages.push({ role: 'user', content: message, message_type: 'text' });
      try {
        const run = await api.createRun({
          session_id: this.sessionId,
          account_id: this.accountId,
          message
        });
        this.runId = run.run_id;
        this.sessionId = run.session_id;
        await this.refreshRun(run.run_id);
        await this.refreshSessions().catch(() => undefined);
        this.listen(run.run_id);
      } catch (error) {
        this.error = normalizeErrorMessage(error);
        this.status = 'failed';
      }
    },

    listen(runId: string) {
      this.activeRunId = runId;
      this.eventSource?.close();
      this.eventSource = null;
      this._stopSseRecoveryPoll();
      this._clearStreamingAssistantMessage();

      const source = new EventSource(api.eventUrl(runId));
      this.eventSource = source;
      let terminalEventReceived = false;

      source.addEventListener('assistant_message_delta', (event) => {
        if (this.activeRunId !== runId) return;
        if (this._hasSeenSseEvent(runId, 'assistant_message_delta', event as MessageEvent)) return;
        try {
          const data = JSON.parse((event as MessageEvent).data) as {
            chunk?: string;
            done?: boolean;
            message_type?: WorkbenchMessage['message_type'];
          };
          this._appendAssistantMessageDelta(
            data.chunk || '',
            Boolean(data.done),
            data.message_type || 'markdown'
          );
        } catch {
          // ignore malformed SSE payload
        }
      });

      source.addEventListener('assistant_message', (event) => {
        if (this.activeRunId !== runId) return;
        if (this._hasSeenSseEvent(runId, 'assistant_message', event as MessageEvent)) return;
        try {
          const data = JSON.parse((event as MessageEvent).data) as {
            content?: string;
            message_type?: WorkbenchMessage['message_type'];
          };
          if (data.content) {
            this._appendAssistantMessage(data.content, data.message_type || 'text');
          }
        } catch {
          // ignore malformed SSE payload
        }
      });

      source.addEventListener('run_completed', async (event) => {
        if (this.activeRunId !== runId) return;
        if (this._hasSeenSseEvent(runId, 'run_completed', event as MessageEvent)) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        await this.refreshRun(runId);
        await this.refreshSessions().catch(() => undefined);
        this._clearStreamingAssistantMessage();
        source.close();
        this.eventSource = null;
      });

      source.addEventListener('run_failed', async (event) => {
        if (this.activeRunId !== runId) return;
        if (this._hasSeenSseEvent(runId, 'run_failed', event as MessageEvent)) return;
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        try {
          const data = JSON.parse((event as MessageEvent).data) as { error?: string };
          this.error = normalizeErrorMessage(data.error || '执行失败');
        } catch {
          this.error = '执行失败';
        }
        await this.refreshRun(runId).catch(() => undefined);
        await this.refreshSessions().catch(() => undefined);
        this._clearStreamingAssistantMessage();
        source.close();
        this.eventSource = null;
      });

      source.addEventListener('close', async () => {
        if (this.activeRunId !== runId) return;
        if (!terminalEventReceived) {
          terminalEventReceived = true;
          await this._recoverFromSseRunState(runId);
        }
        source.close();
        this.eventSource = null;
      });

      source.onerror = async () => {
        if (this.activeRunId !== runId || terminalEventReceived) return;
        terminalEventReceived = true;
        source.close();
        this.eventSource = null;
        await this._recoverFromSseRunState(runId, true);
      };
    },

    _hasSeenSseEvent(runId: string, eventName: string, event: MessageEvent) {
      const rawId = event.lastEventId || event.data;
      const key = `${runId}:${eventName}:${rawId}`;
      if (this.processedSseEventIds.includes(key)) {
        return true;
      }
      this.processedSseEventIds.push(key);
      this.processedSseEventIds = this.processedSseEventIds.slice(-400);
      return false;
    },

    async refreshRun(runId?: string, hydrateMessages = true) {
      const targetRunId = runId || this.runId;
      if (!targetRunId) return;
      this.runInfo = await api.run(targetRunId);
      this.status = this.runInfo.status;
      if (hydrateMessages) {
        await this._hydrateMessages(this.runInfo);
      }
    },

    _appendAssistantMessage(
      content: string,
      messageType: WorkbenchMessage['message_type'] = 'text'
    ) {
      if (!content) return;
      if (this.isStreamingAssistantMessage && this.streamingAssistantMessageIndex >= 0) {
        const target = this.messages[this.streamingAssistantMessageIndex];
        if (target && target.role === 'assistant') {
          target.content = content;
          target.message_type = messageType;
          this.assistantStreamingBuffer = content;
          this._clearStreamingAssistantMessage();
          return;
        }
      }
      if (this.messages.length) {
        const latest = this.messages[this.messages.length - 1];
        if (
          latest.role === 'assistant' &&
          (latest.content === content ||
            latest.content.includes(content.slice(0, 40)) ||
            this.assistantStreamingBuffer === latest.content)
        ) {
          latest.content = content;
          this._clearStreamingAssistantMessage();
          this.assistantStreamingBuffer = content;
          return;
        }
      }
      this.messages.push({ role: 'assistant', content, message_type: messageType });
      this.assistantStreamingBuffer = content;
      this._clearStreamingAssistantMessage();
    },

    _appendAssistantMessageDelta(
      chunk: string,
      done: boolean,
      messageType: WorkbenchMessage['message_type'] = 'markdown'
    ) {
      if (!chunk) {
        if (done) {
          this._clearStreamingAssistantMessage();
        }
        return;
      }

      if (this.isStreamingAssistantMessage && this.streamingAssistantMessageIndex >= 0) {
        const target = this.messages[this.streamingAssistantMessageIndex];
        if (target && target.role === 'assistant') {
          this.assistantStreamingBuffer += chunk;
          target.content = this.assistantStreamingBuffer;
          target.message_type = messageType;
        } else {
          this.messages.push({ role: 'assistant', content: chunk, message_type: messageType });
          this.assistantStreamingBuffer = chunk;
          this.streamingAssistantMessageIndex = this.messages.length - 1;
        }
      } else {
        this.messages.push({ role: 'assistant', content: chunk, message_type: messageType });
        this.assistantStreamingBuffer = chunk;
        this.streamingAssistantMessageIndex = this.messages.length - 1;
        this.isStreamingAssistantMessage = true;
      }
    },

    _clearStreamingAssistantMessage() {
      this.assistantStreamingBuffer = '';
      this.isStreamingAssistantMessage = false;
      this.streamingAssistantMessageIndex = -1;
    },

    async _recoverFromSseRunState(runId: string, connectionError = false) {
      await this.refreshRun(runId).catch(() => undefined);
      const status = this.runInfo?.status;
      if (status === 'running') {
        this._scheduleSseRecovery(runId);
        return;
      }
      this._stopSseRecoveryPoll();
      if (status === 'failed') {
        this.error = normalizeErrorMessage(this.runInfo?.error || '执行失败');
        return;
      }
      if (connectionError && !status) {
        this.error = '会话异常，请重试';
        this.status = 'failed';
      }
    },

    async _hydrateMessages(run: RunInfo) {
      const mapped = run.messages
        .filter((message) => ['text', 'markdown'].includes(message.message_type) && message.role !== 'tool')
        .map((message) => ({
          role: message.role as WorkbenchMessage['role'],
          content: message.content,
          message_type: message.message_type
        }));
      if (mapped.length) {
        this.messages = mapped;
      }
    },

    _stopSseRecoveryPoll() {
      if (this.ssePollTimer) {
        window.clearTimeout(this.ssePollTimer);
        this.ssePollTimer = null;
      }
    },

    _scheduleSseRecovery(runId: string) {
      if (this.ssePollTimer) return;
      const poll = async () => {
        await this.refreshRun(runId).catch(() => undefined);
        this._stopSseRecoveryPoll();
        if (this.runInfo?.status === 'running') {
          this.ssePollTimer = window.setTimeout(() => {
            this._scheduleSseRecovery(runId);
          }, 800);
        }
      };
      this.ssePollTimer = window.setTimeout(poll, 500);
    }
  }
});
