import { defineStore } from 'pinia';
import { api, type Account, type AccountPayload, type AccountUpdatePayload, type Artifact, type RunInfo } from '../services/api';

const LEGACY_RESEARCH_PACK_JSON = '04_research_pack.json';

export interface TimelineEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface TopicCandidate {
  title: string;
  hit_potential: number;
  decision_label: string;
  summary?: string;
  recommended_angle?: string;
}

export interface ResearchPackPreview {
  topicTitle: string;
  markdownText: string;
  summary: string;
  markdownUrl: string;
}

export interface ResearchPackEvent {
  topic_title?: string;
  summary?: string;
  artifact?: {
    json?: string;
    markdown?: string;
  };
}

type ResearchPackPreviewContext = {
  topicTitle: string;
  summary?: string;
};

export const useWorkbenchStore = defineStore('workbench', {
  state: () => ({
    accounts: [] as Account[],
    accountId: '',
    sessionId: '',
    runId: '',
    activeRunId: '',
    status: 'idle',
    messages: [] as Array<{ role: 'user' | 'assistant' | 'system'; content: string }>,
    events: [] as TimelineEvent[],
    runInfo: null as RunInfo | null,
    artifacts: [] as Artifact[],
    error: '',
    pendingTopics: [] as TopicCandidate[],
    pendingHotspotSummary: '',
    pendingResearch: null as ResearchPackPreview | null,
    draftPreview: '',
    ssePollTimer: null as number | null,
    eventSource: null as EventSource | null,
    processedSseEventIds: [] as string[]
  }),
  getters: {
    canSubmit(state) {
      return Boolean(
        state.accountId &&
          state.status !== 'running' &&
          !(
            state.status === 'waiting_for_topic_confirmation' ||
            state.status === 'waiting_for_research_confirmation'
          )
      );
    },
    selectedAccount(state) {
      return state.accounts.find((item) => item.id === state.accountId);
    }
  },
  actions: {
    async boot() {
      await this.refreshAccounts();
      const session = await api.createSession();
      this.sessionId = session.session_id;
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
      this.error = '';
      this.status = 'running';
        this.events = [];
        this.artifacts = [];
        this.draftPreview = '';
        this.pendingTopics = [];
        this.pendingHotspotSummary = '';
        this.pendingResearch = null;
        this.processedSseEventIds = [];
      this.messages.push({ role: 'user', content: message });
      try {
        const run = await api.createRun({
          session_id: this.sessionId,
          account_id: this.accountId,
          message
        });
        this.runId = run.run_id;
        this.sessionId = run.session_id;
        await this.refreshRun(run.run_id);
        this.listen(run.run_id);
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
        this.status = 'failed';
      }
    },
    listen(runId: string) {
      this.activeRunId = runId;
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
      this.error = '';
      const source = new EventSource(api.eventUrl(runId));
      this.eventSource = source;
      let terminalEventReceived = false;
      this._stopSseRecoveryPoll();

      source.addEventListener('assistant_message', (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'assistant_message', event as MessageEvent)) {
          return;
        }
        try {
          const data = JSON.parse((event as MessageEvent).data);
          if (data?.content) {
            this.messages.push({ role: 'assistant', content: String(data.content) });
          }
        } catch {
          // ignore malformed SSE payload
        }
      });

      source.addEventListener('needs_topic_confirmation', (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'needs_topic_confirmation', event as MessageEvent)) {
          return;
        }
      try {
          const data = JSON.parse((event as MessageEvent).data) as {
            topics?: Array<{ title: string; hit_potential: number; decision_label: string; summary?: string }>;
          };
          this.pendingTopics = data.topics ?? [];
          this.pendingTopics = this.pendingTopics.slice(0, 4);
        } catch {
          // ignore malformed SSE payload
        }
      });

      source.addEventListener('needs_hotspot_confirmation', (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'needs_hotspot_confirmation', event as MessageEvent)) {
          return;
        }
        try {
          const data = JSON.parse((event as MessageEvent).data) as { summary?: string };
          this.pendingHotspotSummary = String(data.summary ?? '');
        } catch {
          this.pendingHotspotSummary = '';
        }
      });

      source.addEventListener('research_pack_ready', (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'research_pack_ready', event as MessageEvent)) {
          return;
        }
        try {
          const data = JSON.parse((event as MessageEvent).data) as ResearchPackEvent;
          void this.loadResearchPackPreview(runId, {
            topicTitle: data.topic_title ?? '资料包已就绪',
            summary: data.summary
          }).catch(() => {
            this.pendingResearch = {
              topicTitle: data.topic_title ?? '资料包已就绪',
              summary: data.summary ?? '',
              markdownText: '',
              markdownUrl: ''
            };
          });
        } catch {
          // ignore malformed SSE payload
        }
      });

      source.addEventListener('step_started', (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'step_started', event as MessageEvent)) {
          return;
        }
        try {
          this.events.push({ event: 'step_started', data: JSON.parse((event as MessageEvent).data) });
        } catch {
          // ignore malformed SSE payload
        }
      });
      source.addEventListener('step_completed', (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'step_completed', event as MessageEvent)) {
          return;
        }
        try {
          this.events.push({ event: 'step_completed', data: JSON.parse((event as MessageEvent).data) });
        } catch {
          // ignore malformed SSE payload
        }
      });
      source.addEventListener('run_completed', async (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'run_completed', event as MessageEvent)) {
          return;
        }
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        try {
          const data = JSON.parse((event as MessageEvent).data);
          const draft = (data as { draft?: string; draft_preview?: string }).draft;
          if (typeof draft === 'string') {
            this.draftPreview = draft;
          }
        } catch {
          // no draft payload required for now
        }
      await this.refreshRun(runId);
      this.status = 'completed';
      source.close();
      this.eventSource = null;
      });
      source.addEventListener('run_failed', async (event) => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (this._hasSeenSseEvent(runId, 'run_failed', event as MessageEvent)) {
          return;
        }
        terminalEventReceived = true;
        this._stopSseRecoveryPoll();
        try {
          const data = JSON.parse((event as MessageEvent).data);
          this.error = (data as { error?: string }).error ?? '执行失败';
        } catch {
          this.error = '执行失败';
        }
        if (this.error) {
          this.messages.push({ role: 'assistant', content: `执行失败：${this.error}` });
        }
        await this.refreshRun(runId);
        this.status = 'failed';
        source.close();
        this.eventSource = null;
      });
      source.addEventListener('close', async () => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (terminalEventReceived) {
          source.close();
          this.eventSource = null;
          return;
        }
        terminalEventReceived = true;
        await this._recoverFromSseRunState(runId);
        source.close();
        this.eventSource = null;
      });
      source.onerror = async () => {
        if (this.activeRunId !== runId) {
          return;
        }
        if (terminalEventReceived) return;
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
      return false;
    },
    async selectTopic(index: number) {
      if (!this.runId) return;
      this.error = '';
      const run = await api.selectTopic(this.runId, index);
      this.pendingTopics = [];
      this.pendingResearch = null;
      this.runInfo = run;
      this.status = run.status;
      this.listen(this.runId);
    },
    async confirmHotspots() {
      if (!this.runId) return;
      this.error = '';
      const run = await api.confirmHotspots(this.runId);
      this.pendingHotspotSummary = '';
      this.pendingResearch = null;
      this.runInfo = run;
      this.status = run.status;
      this.listen(this.runId);
    },
    async continueWithTopTopic() {
      if (!this.pendingTopics.length) return;
      await this.selectTopic(0);
    },
    async confirmResearch() {
      if (!this.runId) return;
      this.error = '';
      const run = await api.confirmResearch(this.runId);
      this.status = run.status;
      this.pendingResearch = null;
      this.runInfo = run;
      this.listen(this.runId);
    },
    async refreshRun(runId?: string) {
      const targetRunId = runId || this.runId;
      if (!targetRunId) return;
      this.runInfo = await api.run(targetRunId);
      this.artifacts = this.runInfo.artifacts;
      this.status = this.runInfo.status;
      await this._hydratePendingState(this.runInfo);
      if (this.runInfo.status === 'completed' || this.runInfo.status === 'failed') {
        this.pendingTopics = [];
        this.pendingResearch = null;
        this.pendingHotspotSummary = '';
      }
    },

    async _recoverFromSseRunState(runId: string, connectionError = false) {
      await this.refreshRun(runId).catch(() => undefined);
      const status = this.runInfo?.status;

      if (status === 'running') {
        this.error = '';
        this._scheduleSseRecovery(runId);
        return;
      }

      this._stopSseRecoveryPoll();

      if (status === 'waiting_for_topic_confirmation' || status === 'waiting_for_research_confirmation') {
        this.error = '';
        return;
      }

      if (status === 'failed') {
        const message = this.runInfo?.error || '执行失败';
        if (this.error !== message) {
          this.error = message;
          this.messages.push({ role: 'assistant', content: `执行失败：${message}` });
        }
        this.status = 'failed';
        return;
      }

      if (status === 'completed') {
        this.status = 'completed';
        this.error = '';
        return;
      }

      if (connectionError) {
        this.error = '事件流异常，请刷新后重试。';
        this.status = 'failed';
      }
    },

    async _hydratePendingState(run: RunInfo) {
      if (run.status === 'waiting_for_topic_confirmation') {
        this.pendingHotspotSummary = '';
        try {
          const payload = JSON.parse(run.pending_payload || '[]');
          if (Array.isArray(payload)) {
            this.pendingTopics = payload.slice(0, 4) as TopicCandidate[];
            return;
          }
          if (typeof payload === 'object' && payload !== null && 'mode' in payload) {
            const maybe = payload as { mode?: unknown; summary?: unknown };
            if (maybe.mode === 'collect_hotspots_confirmation' && typeof maybe.summary === 'string') {
              this.pendingHotspotSummary = maybe.summary;
              return;
            }
          }
        } catch {
          this.pendingTopics = [];
        }
        this.pendingTopics = [];
        this.pendingResearch = null;
        this.pendingHotspotSummary = '';
        return;
      }

      if (run.status === 'waiting_for_research_confirmation') {
        await this.loadResearchPackPreview(run.id, {
          topicTitle: run.selected_topic_title || '资料包已就绪'
        });
        return;
      }

      this.pendingResearch = null;
      this.pendingHotspotSummary = '';
    },

    async loadResearchPackPreview(runId: string, context: ResearchPackPreviewContext) {
      const run = await api.run(runId);
      const researchArtifact = run.artifacts.find(
        (artifact) =>
          artifact.kind === 'research_pack_markdown' ||
          artifact.title.includes('04_深度检索.md') ||
          /04_深度检索\.md/.test(artifact.title) ||
          /research_pack_markdown/i.test(artifact.title)
      );
      if (!researchArtifact) {
        this.pendingResearch = {
          topicTitle: context.topicTitle,
          summary: context.summary ?? '',
          markdownText: '未检测到资料包 md 文件，请稍后刷新后重试。',
          markdownUrl: ''
        };
        return;
      }
      const markdownUrl = api.artifactUrl(researchArtifact);
      const summaryArtifact = run.artifacts.find(
        (artifact) =>
          artifact.kind === 'research_pack_json' ||
          artifact.title.includes('04_深度检索.json') ||
          /04_深度检索\.json/.test(artifact.title) ||
          /research_pack_json/i.test(artifact.title) ||
          artifact.title === LEGACY_RESEARCH_PACK_JSON
      );
      let summary = context.summary?.trim() ?? '';
      if (!summary && summaryArtifact) {
        const response = await fetch(api.artifactUrl(summaryArtifact));
        if (response.ok) {
          try {
            const payload = await response.json();
            if (payload && typeof payload.summary === 'string') {
              summary = payload.summary.trim();
            }
          } catch {
            // ignore malformed json summary artifact
          }
        }
      }
      const response = await fetch(markdownUrl);
      if (!response.ok) {
        this.pendingResearch = {
          topicTitle: context.topicTitle,
          summary,
          markdownText: '资料包读取失败，请稍后重试。',
          markdownUrl
        };
        return;
      }
      const markdownText = await response.text();
      this.pendingResearch = {
        topicTitle: context.topicTitle,
        summary,
        markdownText,
        markdownUrl
      };
    },

    _stopSseRecoveryPoll() {
      if (this.ssePollTimer) {
        window.clearTimeout(this.ssePollTimer);
        this.ssePollTimer = null;
      }
    },

    _scheduleSseRecovery(runId: string) {
      if (this.ssePollTimer) {
        return;
      }

      const poll = async () => {
        await this.refreshRun(runId).catch(() => undefined);
        this._stopSseRecoveryPoll();

        if (this.runInfo?.status === 'running') {
          this.ssePollTimer = window.setTimeout(() => {
            this._scheduleSseRecovery(runId);
          }, 800);
        } else if (this.runInfo?.status === 'failed') {
          this.error = this.runInfo.error || '执行失败';
          this.messages.push({ role: 'assistant', content: `执行失败：${this.error}` });
        } else if (this.runInfo?.status === 'completed') {
          // keep completed state from refreshRun
        }
      };

      this.ssePollTimer = window.setTimeout(poll, 500);
    }
  }
});

