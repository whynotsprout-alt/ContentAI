import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { ApiError, FetchEventStream, api } from '../src/shared/services/api';
import {
  AgentCatalogRefreshCancelledError,
  useWorkbenchStore
} from '../src/features/workbench/stores/workbench.store';

const apiState = (() => {
  const sessionState = new Map();
  const streamBySession = new Map();
  let executionCounter = 0;

  class FakeFetchEventStream {
    listeners = new Map();
    onerror = null;
    closed = false;
    eventCounter = 0;

    constructor(sessionId, afterSequence = 0) {
      this.sessionId = sessionId;
      this.eventCounter = afterSequence;
    }

    addEventListener(name, listener) {
      const handlers = this.listeners.get(name) ?? [];
      handlers.push(listener);
      this.listeners.set(name, handlers);
    }

    close() {
      this.closed = true;
      this.listeners.clear();
    }

    async dispatch(name, payload, lastEventId) {
      const sequence = ++this.eventCounter;
      const executionId = payload?.execution_id ?? '';
      const attemptId = payload?.attempt_id ?? null;
      const messageId = payload?.message_id ?? null;
      const toolCallId = payload?.tool_call_id ?? null;
      const timestamp = payload?.timestamp ?? '2026-01-01T00:00:00.000Z';
      const namespace = Array.isArray(payload?.namespace) ? payload.namespace : [];
      const data = { ...(payload ?? {}) };
      for (const key of [
        'schema_version', 'execution_id', 'sequence', 'event_id', 'channel', 'namespace',
        'attempt_id', 'message_id', 'tool_call_id', 'timestamp', 'session_id', 'thread_id',
        'event_type', 'type'
      ]) delete data[key];
      const envelope = payload && typeof payload === 'object' && payload.schema_version === 3
        ? payload
        : {
            schema_version: 3,
            execution_id: executionId,
            sequence,
            event_id: `${executionId}:${sequence}`,
            channel: name,
            namespace,
            attempt_id: attemptId,
            message_id: messageId,
            tool_call_id: toolCallId,
            timestamp,
            data
          };
      const event = {
        data: JSON.stringify(envelope),
        lastEventId: lastEventId ?? envelope.event_id,
        type: name
      };
      for (const handler of this.listeners.get(name) ?? []) {
        await handler(event);
      }
    }
  }

  return {
    sessionState,
    streamBySession,
    executionCounter,
    FakeFetchEventStream,
    nextExecutionId() {
      return `exe-${++executionCounter}`;
    },
    reset() {
      sessionState.clear();
      streamBySession.clear();
      executionCounter = 0;
    }
  };
})();

function installApiMock() {
  Object.assign(api, {
      agents: async () => ({
        items: [
          {
            id: 'acc-1',
            name: 'demo',
            description: '',
            current_version: { id: 'acc-1-v1', agent_id: 'acc-1', version: 1 }
          }
        ],
        next_cursor: null
      }),
      agent: async () => ({
        id: 'acc-1',
        name: 'demo',
        description: '',
        current_version: {
          id: 'acc-1-v1',
          agent_id: 'acc-1',
          version: 1,
          topic_scoring_prompt: '',
          content_prompt: '',
          hotspot_sources: []
        }
      }),
      createAgent: async (payload) => ({ id: 'acc-1', ...payload }),
      updateAgent: async (id, payload) => ({ id, ...payload }),
      deleteAgent: async () => undefined,
      sessions: async ({ agent_id: requestedAgentId } = {}) => {
        const items = Array.from(apiState.sessionState.values()).map((session) => ({
          session_id: session.session_id,
          agent_id: session.agent_id,
          title: session.title,
          created_at: session.created_at,
          updated_at: session.updated_at,
          latest_execution_status: session.latest_execution_status,
          message_count: session.messages.length
        }));
        return {
          items: requestedAgentId ? items.filter((session) => session.agent_id === requestedAgentId) : items,
          next_cursor: null
        };
      },
      session: async (sessionId) => apiState.sessionState.get(sessionId),
      createSession: async () => {
        setSession('session-new', {
          title: 'new',
          latest_execution_status: 'idle',
          latest_execution: null,
          messages: []
        });
        return {
          session_id: 'session-new',
          agent_id: 'acc-1',
          title: 'new'
        };
      },
      deleteSession: async () => undefined,
      sendMessage: async (sessionId, payload) => {
        const executionId = apiState.nextExecutionId();
        const messageId = `msg-${executionId}`;
        const session = apiState.sessionState.get(sessionId);
        if (session) {
          session.messages.push({
            id: messageId,
            role: 'user',
            message_type: 'text',
            content: payload.message,
            created_at: '2026-01-01T00:00:01.000Z'
          });
          session.latest_execution = {
            id: executionId,
            invocation_id: `inv-${executionId}`,
            session_id: sessionId,
            agent_id: 'acc-1',
            user_message: 'Question',
            status: 'running',
            error: ''
          };
          session.latest_execution_status = 'running';
        }
        return {
          session_id: sessionId,
          message_id: messageId,
          execution_id: executionId,
          status: 'pending'
        };
      },
      executionEvents: (executionId, afterSequence = 0) => {
        const session = Array.from(apiState.sessionState.values()).find(
          (item) => item.latest_execution?.id === executionId
        );
        const sessionId = session?.session_id ?? '';
        const stream = new apiState.FakeFetchEventStream(sessionId, afterSequence);
        apiState.streamBySession.set(sessionId, stream);
        return stream;
      },
      cancelRun: async (executionId) => {
        for (const session of apiState.sessionState.values()) {
          if (session.latest_execution?.id !== executionId) continue;
          session.latest_execution.status = 'cancelled';
          session.latest_execution_status = 'cancelled';
          return { id: executionId, session_id: session.session_id, status: 'cancelled' };
        }
        throw new Error('run not found');
      }
  });
}

function initStore() {
  setActivePinia(createPinia());
  const store = useWorkbenchStore();
  store.agentId = 'acc-1';
  return store;
}

function setSession(sessionId, patch = {}) {
  apiState.sessionState.set(sessionId, {
    session_id: sessionId,
    agent_id: 'acc-1',
    title: `session-${sessionId}`,
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    latest_execution_status: 'idle',
    messages: [],
    next_cursor: null,
    latest_execution: {
      id: 'exe-0',
      session_id: sessionId,
      status: 'idle',
      error: ''
    },
    ...patch
  });
}

function historyMessage(index, sessionId = 's1') {
  return {
    id: `${sessionId}-message-${String(index).padStart(4, '0')}`,
    role: index % 2 ? 'user' : 'assistant',
    message_type: index % 2 ? 'text' : 'markdown',
    content: `${sessionId} history ${index}`,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString()
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

async function settleAsyncActions() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  if (typeof globalThis.window === 'undefined') {
    globalThis.window = globalThis;
  }
  if (!globalThis.window.setTimeout) {
    globalThis.window.setTimeout = setTimeout;
    globalThis.window.clearTimeout = clearTimeout;
    globalThis.window.setInterval = setInterval;
    globalThis.window.clearInterval = clearInterval;
  }
  apiState.reset();
  installApiMock();
  setActivePinia(createPinia());
});
describe('workbench cross-session recovery', () => {
  it('loads trimmed agent pages, deduplicates ids, and commits the catalog atomically', async () => {
    const calls = [];
    api.agents = async (params, signal) => {
      calls.push({ params, signal });
      if (!params.cursor) {
        return {
          items: [
            { id: 'agent-a', name: 'first value', description: '', current_version: null }
          ],
          next_cursor: '  next-page  '
        };
      }
      return {
        items: [
          { id: 'agent-a', name: 'later duplicate', description: '', current_version: null },
          { id: 'agent-b', name: 'second agent', description: '', current_version: null }
        ],
        next_cursor: null
      };
    };
    const store = initStore();
    store.agents = [
      { id: 'old-agent', name: 'old catalog', description: '', current_version: null }
    ];
    store.agentId = 'old-agent';

    expect(await store.refreshAgents('agent-b')).toBe(true);

    expect(calls.map(({ params }) => params)).toEqual([
      { cursor: undefined, limit: 200 },
      { cursor: 'next-page', limit: 200 }
    ]);
    expect(calls.every(({ signal }) => signal instanceof AbortSignal)).toBe(true);
    expect(store.agents.map((agent) => agent.id)).toEqual(['agent-a', 'agent-b']);
    expect(store.agents[0]?.name).toBe('first value');
    expect(store.agentId).toBe('agent-b');
  });

  it('keeps the previous agent catalog on repeated cursors and page failures', async () => {
    const store = initStore();
    const previous = [
      { id: 'old-agent', name: 'old catalog', description: '', current_version: null }
    ];
    store.agents = previous;
    store.agentId = 'old-agent';
    api.agents = async ({ cursor }) => ({
      items: [{ id: cursor ? 'agent-b' : 'agent-a', name: 'partial', description: '', current_version: null }],
      next_cursor: ' repeated-cursor '
    });

    await expect(store.refreshAgents()).rejects.toThrow('repeated cursor');
    expect(store.agents).toEqual(previous);
    expect(store.agentId).toBe('old-agent');

    let calls = 0;
    api.agents = async () => {
      calls += 1;
      if (calls === 2) throw new Error('page failed');
      return {
        items: [{ id: 'agent-a', name: 'partial', description: '', current_version: null }],
        next_cursor: 'next-page'
      };
    };
    await expect(store.refreshAgents()).rejects.toThrow('page failed');
    expect(store.agents).toEqual(previous);
    expect(store.agentId).toBe('old-agent');
  });

  it('enforces the agent catalog caps and accepts the exact safe boundary', async () => {
    const store = initStore();
    const previous = [
      { id: 'old-agent', name: 'old catalog', description: '', current_version: null }
    ];
    store.agents = previous;
    let pageCalls = 0;
    api.agents = async () => {
      const page = pageCalls;
      pageCalls += 1;
      return {
        items: Array.from({ length: 200 }, (_, index) => ({
          id: `agent-${page}-${index}`,
          name: 'catalog agent',
          description: '',
          current_version: null
        })),
        next_cursor: `cursor-${pageCalls}`
      };
    };

    await expect(store.refreshAgents()).rejects.toThrow('safe page limit');
    expect(pageCalls).toBe(100);
    expect(store.agents).toEqual(previous);

    api.agents = async () => ({
      items: Array.from({ length: 20_001 }, (_, index) => ({
        id: `oversized-agent-${index}`,
        name: 'oversized catalog agent',
        description: '',
        current_version: null
      })),
      next_cursor: null
    });
    await expect(store.refreshAgents()).rejects.toThrow('safe item limit');
    expect(store.agents).toEqual(previous);

    pageCalls = 0;
    api.agents = async () => {
      const page = pageCalls;
      pageCalls += 1;
      return {
        items: Array.from({ length: 200 }, (_, index) => ({
          id: `boundary-agent-${page}-${index}`,
          name: 'boundary catalog agent',
          description: '',
          current_version: null
        })),
        next_cursor: pageCalls < 100 ? `boundary-cursor-${pageCalls}` : null
      };
    };
    expect(await store.refreshAgents()).toBe(true);
    expect(pageCalls).toBe(100);
    expect(store.agents).toHaveLength(20_000);
  });

  it('aborts an older agent refresh and ignores its late response', async () => {
    const stalePage = deferred();
    const signals = [];
    let calls = 0;
    api.agents = async (_params, signal) => {
      signals.push(signal);
      calls += 1;
      if (calls === 1) return stalePage.promise;
      return {
        items: [
          { id: 'current-agent', name: 'current catalog', description: '', current_version: null }
        ],
        next_cursor: null
      };
    };
    const store = initStore();
    const staleRefresh = store.refreshAgents();
    const currentRefresh = store.refreshAgents();

    expect(signals[0]?.aborted).toBe(true);
    expect(await currentRefresh).toBe(true);
    stalePage.resolve({
      items: [
        { id: 'stale-agent', name: 'stale catalog', description: '', current_version: null }
      ],
      next_cursor: null
    });
    expect(await staleRefresh).toBe(false);

    expect(store.agents.map((agent) => agent.id)).toEqual(['current-agent']);
    expect(store.agentId).toBe('current-agent');
  });

  it('stops boot after dispose aborts an agent refresh and its response arrives late', async () => {
    const latePage = deferred();
    let signal;
    api.agents = async (_params, requestSignal) => {
      signal = requestSignal;
      return latePage.promise;
    };
    const store = initStore();
    store.agents = [
      { id: 'old-agent', name: 'old catalog', description: '', current_version: null }
    ];
    store.agentId = 'old-agent';
    let sessionRefreshes = 0;
    let sessionStarts = 0;
    store.refreshSessions = async () => {
      sessionRefreshes += 1;
    };
    store.startNewSession = async () => {
      sessionStarts += 1;
    };
    const boot = store.boot();

    store.dispose();
    expect(signal?.aborted).toBe(true);
    expect(store.agentRefreshController).toBeNull();
    latePage.resolve({
      items: [
        { id: 'late-agent', name: 'late catalog', description: '', current_version: null }
      ],
      next_cursor: null
    });
    await boot;

    expect(store.agents.map((agent) => agent.id)).toEqual(['old-agent']);
    expect(store.agentId).toBe('old-agent');
    expect(sessionRefreshes).toBe(0);
    expect(sessionStarts).toBe(0);
  });

  it('does not let a superseded boot continue after a newer boot completes', async () => {
    const stalePage = deferred();
    const signals = [];
    let catalogRequests = 0;
    api.agents = async (_params, signal) => {
      signals.push(signal);
      catalogRequests += 1;
      if (catalogRequests === 1) return stalePage.promise;
      return {
        items: [
          { id: 'current-agent', name: 'current catalog', description: '', current_version: null }
        ],
        next_cursor: null
      };
    };
    const store = initStore();
    let sessionRefreshes = 0;
    let sessionStarts = 0;
    store.refreshSessions = async () => {
      sessionRefreshes += 1;
    };
    store.startNewSession = async () => {
      sessionStarts += 1;
    };

    const staleBoot = store.boot();
    const currentBoot = store.boot();
    expect(signals[0]?.aborted).toBe(true);
    await currentBoot;
    stalePage.resolve({
      items: [
        { id: 'stale-agent', name: 'stale catalog', description: '', current_version: null }
      ],
      next_cursor: null
    });
    await staleBoot;

    expect(store.agents.map((agent) => agent.id)).toEqual(['current-agent']);
    expect(sessionRefreshes).toBe(1);
    expect(sessionStarts).toBe(1);
  });

  it('uses a unique brand for internal agent catalog cancellation', () => {
    const cancellation = new AgentCatalogRefreshCancelledError();
    const externalAbort = new DOMException('external request cancelled', 'AbortError');
    const sameNamedError = new Error('unrelated error');
    sameNamedError.name = cancellation.name;

    expect(cancellation).toBeInstanceOf(AgentCatalogRefreshCancelledError);
    expect(externalAbort).not.toBeInstanceOf(AgentCatalogRefreshCancelledError);
    expect(sameNamedError).not.toBeInstanceOf(AgentCatalogRefreshCancelledError);
  });

  it('stops mutation action follow-ups when the catalog refresh returns false', async () => {
    const profile = {
      id: 'agent-a',
      name: 'Agent A',
      description: 'Agent A description',
      current_version: null
    };
    const version = {
      id: 'agent-a-v2',
      agent_id: 'agent-a',
      version: 2,
      topic_scoring_prompt: 'score',
      content_prompt: 'content',
      hotspot_sources: ['weibo']
    };
    const profilePayload = {
      name: profile.name,
      description: profile.description,
      topic_scoring_prompt: 'score',
      content_prompt: 'content',
      hotspot_sources: ['weibo']
    };
    const store = initStore();
    store.agents = [profile];
    store.agentId = profile.id;
    store.refreshAgents = async () => false;
    let agentSelections = 0;
    let detailRequests = 0;
    let versionWrites = 0;
    let postDeleteReads = 0;
    let sessionRefreshes = 0;
    let sessionStarts = 0;
    store.chooseAgent = async () => {
      agentSelections += 1;
      return true;
    };
    store.refreshSessions = async () => {
      sessionRefreshes += 1;
    };
    store.startNewSession = async () => {
      sessionStarts += 1;
    };
    api.createAgent = async () => profile;
    api.updateAgent = async () => profile;
    api.createAgentVersion = async () => {
      versionWrites += 1;
      return version;
    };
    api.agent = async () => {
      detailRequests += 1;
      return { ...profile, current_version: version };
    };
    api.deleteAgent = async () => undefined;

    await expect(store.createAgent(profilePayload)).rejects.toBeInstanceOf(
      AgentCatalogRefreshCancelledError
    );
    await expect(
      store.updateAgent(profile.id, { name: 'renamed' }).then(() =>
        store.createAgentVersion(profile.id, {
          topic_scoring_prompt: 'must not write',
          content_prompt: 'must not write',
          hotspot_sources: []
        })
      )
    ).rejects.toBeInstanceOf(AgentCatalogRefreshCancelledError);
    expect(versionWrites).toBe(0);
    await expect(
      store.createAgentVersion(profile.id, {
        topic_scoring_prompt: 'score',
        content_prompt: 'content',
        hotspot_sources: ['weibo']
      })
    ).rejects.toBeInstanceOf(AgentCatalogRefreshCancelledError);
    expect(versionWrites).toBe(1);
    await expect(
      store.deleteAgent(profile.id).then(() => {
        postDeleteReads += 1;
        return api.agent(profile.id);
      })
    ).rejects.toBeInstanceOf(AgentCatalogRefreshCancelledError);

    expect(agentSelections).toBe(0);
    expect(detailRequests).toBe(0);
    expect(postDeleteReads).toBe(0);
    expect(sessionRefreshes).toBe(0);
    expect(sessionStarts).toBe(0);
  });

  it('loads more than 50 messages across pages, deduplicated in chronological order', async () => {
    const messages = Array.from({ length: 125 }, (_, index) => historyMessage(index + 1));
    setSession('s1');
    const base = apiState.sessionState.get('s1');
    const calls = [];
    api.session = async (sessionId, params = {}) => {
      calls.push({ sessionId, ...params });
      if (params.cursor === 'older-page') {
        return {
          ...base,
          messages: messages.slice(0, 66),
          next_cursor: null,
          message_count: messages.length
        };
      }
      return {
        ...base,
        messages: messages.slice(65),
        next_cursor: 'older-page',
        message_count: messages.length
      };
    };
    const store = initStore();

    expect(await store.loadSession('s1')).toBe(true);

    expect(calls).toEqual([
      { sessionId: 's1', cursor: undefined, limit: 200 },
      { sessionId: 's1', cursor: 'older-page', limit: 200 }
    ]);
    expect(store.messages).toHaveLength(125);
    expect(store.messages.map((message) => message.id)).toEqual(
      messages.map((message) => message.id)
    );
    expect(new Set(store.messages.map((message) => message.id)).size).toBe(125);
    expect(store.messages[0]?.content).toBe('s1 history 1');
    expect(store.messages.at(-1)?.content).toBe('s1 history 125');
  });

  it('preserves a realtime message added while older history is loading', async () => {
    const messages = Array.from({ length: 90 }, (_, index) => historyMessage(index + 1));
    const olderPage = deferred();
    const olderPageRequested = deferred();
    setSession('s1');
    const base = apiState.sessionState.get('s1');
    api.session = async (_sessionId, params = {}) => {
      if (!params.cursor) {
        return { ...base, messages: messages.slice(45), next_cursor: 'older-page' };
      }
      olderPageRequested.resolve();
      return olderPage.promise;
    };
    const store = initStore();
    const loading = store.loadSession('s1');
    await olderPageRequested.promise;
    store.messages.push({
      role: 'assistant',
      message_type: 'markdown',
      content: 'live partial output',
      assistant_state: 'streaming'
    });
    olderPage.resolve({ ...base, messages: messages.slice(0, 45), next_cursor: null });

    expect(await loading).toBe(true);
    expect(store.messages).toHaveLength(91);
    expect(store.messages.slice(0, 90).map((message) => message.id)).toEqual(
      messages.map((message) => message.id)
    );
    expect(store.messages.at(-1)?.content).toBe('live partial output');
    expect(store.messages.at(-1)?.assistant_state).toBe('streaming');
  });

  it('does not commit stale history pages after switching sessions', async () => {
    const staleOlderPage = deferred();
    const staleOlderPageRequested = deferred();
    const s1Messages = Array.from({ length: 80 }, (_, index) => historyMessage(index + 1, 's1'));
    const s2Messages = Array.from({ length: 3 }, (_, index) => historyMessage(index + 1, 's2'));
    setSession('s1');
    setSession('s2');
    const s1 = apiState.sessionState.get('s1');
    const s2 = apiState.sessionState.get('s2');
    api.session = async (sessionId, params = {}) => {
      if (sessionId === 's2') {
        return { ...s2, messages: s2Messages, next_cursor: null };
      }
      if (!params.cursor) {
        return { ...s1, messages: s1Messages.slice(40), next_cursor: 's1-older' };
      }
      staleOlderPageRequested.resolve();
      return staleOlderPage.promise;
    };
    const store = initStore();
    const staleLoad = store.loadSession('s1');
    await staleOlderPageRequested.promise;

    expect(await store.loadSession('s2')).toBe(true);
    staleOlderPage.resolve({ ...s1, messages: s1Messages.slice(0, 40), next_cursor: null });

    expect(await staleLoad).toBe(false);
    expect(store.sessionId).toBe('s2');
    expect(store.messages.map((message) => message.id)).toEqual(
      s2Messages.map((message) => message.id)
    );
    expect(store.isLoadingSession).toBe(false);
  });

  it('consumes actual V3 SSE frames and advances a composite event cursor', async () => {
    const executionId = 'exe-contract';
    setSession('s1', {
      latest_execution_status: 'completed',
      latest_execution: {
        id: executionId,
        session_id: 's1',
        agent_id: 'acc-1',
        status: 'completed',
        error: ''
      },
      messages: [
        {
          id: 'a-contract',
          role: 'assistant',
          message_type: 'markdown',
          content: '实时结果',
          created_at: '2026-01-01T00:00:00.000Z'
        }
      ]
    });
    const store = initStore();
    store.sessionId = 's1';
    store.executionId = executionId;
    store.runLifecycle = 'queued';
    store._ensureAssistantPlaceholder();

    const frames = [
      ['lifecycle', 1, { name: 'run_start', content: '' }],
      ['messages', 2, { name: 'assistant_message_delta', content: '实时', chunk: '实时', message_type: 'markdown', done: false }],
      ['tools', 3, { name: 'tool_progress', tool_name: 'fetch_hotspots', status: 'running', progress: { stage: 'scoring_topics', label: '正在使用选题评分提示词评估热点', candidate_count: 60 } }],
      ['messages', 4, { name: 'assistant_message_delta', content: '结果', chunk: '结果', message_type: 'markdown', done: true }],
      ['lifecycle', 5, { name: 'run_finish', content: '' }]
    ].map(([channel, sequence, data]) => {
      const envelope = {
        schema_version: 3,
        execution_id: executionId,
        sequence,
        event_id: `${executionId}:${sequence}`,
        channel,
        namespace: [],
        attempt_id: 'att-contract',
        message_id: null,
        tool_call_id: null,
        timestamp: '2026-01-01T00:00:00Z',
        data
      };
      return `id: ${envelope.event_id}\nevent: ${channel}\ndata: ${JSON.stringify(envelope)}\n\n`;
    }).join('');

    const originalFetch = globalThis.fetch;
    const originalDocument = globalThis.document;
    globalThis.document = { cookie: '' };
    globalThis.fetch = async () => new Response(frames, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' }
    });
    try {
      store.listen('s1', store.sessionContextToken, new FetchEventStream('/contract/events'));
      await new Promise((resolve) => setTimeout(resolve, 30));
    } finally {
      globalThis.fetch = originalFetch;
      globalThis.document = originalDocument;
    }

    expect(store.lastEventSequence).toBe(5);
    expect(store.runLifecycle).toBe('completed');
    expect(store.messages.at(-1)?.content).toBe('实时结果');
    expect(store.events.some((event) => event.event === 'tool_progress')).toBe(true);
  });

  it('shows pending, streams tokens, and writes the final assistant content', async () => {
    setSession('s1', {
      latest_execution_status: 'idle',
      messages: [
        {
          id: 'm1',
          role: 'user',
          message_type: 'text',
          content: 'History question',
          created_at: '2026-01-01T00:00:00.000Z'
        }
      ]
    });

    const store = initStore();
    store.sessionId = 's1';
    store.sessions = [
      {
        session_id: 's1',
        title: 'Session 1',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_execution_status: 'idle',
        message_count: 1
      }
    ];

    await store.submit('Please start generating');

    const pending = store.messages[store.messages.length - 1];
    expect(store.runLifecycle).toBe('queued');
    expect(pending?.role).toBe('assistant');
    expect(pending?.assistant_state).toBe('pending');
    expect(
      store.messages.find((item) => item.role === 'user' && item.content === 'Please start generating')
        ?.assistant_state
    ).toBe('normal');

    const stream = apiState.streamBySession.get('s1');
    const executionId = apiState.sessionState.get('s1')?.latest_execution?.id;
    expect(stream?.closed).toBe(false);

    await stream?.dispatch('lifecycle', {
      name: 'run_start',
      content: 'run_start',
      session_id: 's1',
      execution_id: executionId
    });
    expect(store.runLifecycle).toBe('running');
    expect(store.assistantStreamingState).toBe('streaming');

    await stream?.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'Hello',
      session_id: 's1',
      execution_id: executionId,
      chunk: 'Hello'
    });
    await stream?.dispatch('messages', {
      name: 'assistant_message_delta',
      content: ' world',
      session_id: 's1',
      execution_id: executionId,
      chunk: ' world'
    });
    await settleAsyncActions();
    const mid = store.messages[store.messages.length - 1];
    expect(mid.content).toBe('Hello world');
    expect(mid.assistant_state).toBe('streaming');

    setSession('s1', {
      ...apiState.sessionState.get('s1'),
      latest_execution_status: 'completed',
      latest_execution: {
        ...apiState.sessionState.get('s1').latest_execution,
        status: 'completed'
      },
      messages: [
        ...apiState.sessionState.get('s1').messages,
        {
          id: 'a1',
          role: 'assistant',
          message_type: 'text',
          content: 'Hello world',
          created_at: '2026-01-01T00:00:02.000Z'
        }
      ]
    });

    await stream?.dispatch('messages', {
      name: 'assistant_message',
      session_id: 's1',
      execution_id: executionId,
      content: 'Hello world',
      message_id: 'a1',
      timestamp: '2026-01-01T00:00:02.000Z'
    });
    expect(store.messages.at(-1)?.content).toBe('Hello world');
    expect(store.messages.at(-1)?.id).toBe('a1');
    expect(store.messages.at(-1)?.assistant_state).toBe('normal');
    const messageCountAfterFinalSnapshot = store.messages.length;
    await stream?.dispatch('messages', {
      name: 'assistant_message',
      session_id: 's1',
      execution_id: executionId,
      content: 'Hello world',
      message_id: 'a1',
      timestamp: '2026-01-01T00:00:02.000Z'
    });
    expect(store.messages).toHaveLength(messageCountAfterFinalSnapshot);
    await stream?.dispatch('lifecycle', {
      name: 'run_finish',
      session_id: 's1',
      execution_id: executionId
    });

    await settleAsyncActions();

    expect(store.runLifecycle).toBe('completed');
    const last = store.messages[store.messages.length - 1];
    expect(last.role).toBe('assistant');
    expect(last.content).toBe('Hello world');
    expect(last.assistant_state).toBe('normal');
  });

  it('discards stale partial output when a recovered worker starts a retry attempt', async () => {
    const executionId = 'exe-retry';
    setSession('s1', {
      latest_execution_status: 'running',
      latest_execution: {
        id: executionId,
        session_id: 's1',
        agent_id: 'acc-1',
        status: 'running',
        error: ''
      }
    });
    const store = initStore();
    store.sessionId = 's1';
    store.executionId = executionId;
    store.runLifecycle = 'running';
    store._ensureAssistantPlaceholder();
    const stream = new apiState.FakeFetchEventStream('s1');
    store.listen('s1', store.sessionContextToken, stream);

    await stream.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'stale partial',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-1'
    });
    expect(store.messages.at(-1)?.content).toBe('stale partial');

    await stream.dispatch('lifecycle', {
      name: 'run_retry',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-2'
    });
    expect(store.messages.at(-1)?.content).toBe('');

    await stream.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'late stale output',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-1'
    });
    expect(store.messages.at(-1)?.content).toBe('');

    await stream.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'fresh output',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-2'
    });
    expect(store.messages.at(-1)?.content).toBe('fresh output');
    expect(store.messages.at(-1)?.content).not.toContain('stale partial');
    expect(store.messages.at(-1)?.content).not.toContain('late stale output');

    await stream.dispatch('messages', {
      name: 'assistant_message_delta',
      content: ' plus compatible output',
      session_id: 's1',
      execution_id: executionId
    });
    expect(store.messages.at(-1)?.content).toBe('fresh output plus compatible output');
  });

  it('resets stale partial output when a new attempt_start arrives without run_retry', async () => {
    const executionId = 'exe-attempt-start';
    setSession('s1', {
      latest_execution_status: 'running',
      latest_execution: {
        id: executionId,
        session_id: 's1',
        agent_id: 'acc-1',
        status: 'running',
        error: ''
      }
    });
    const store = initStore();
    store.sessionId = 's1';
    store.executionId = executionId;
    store.runLifecycle = 'running';
    store._ensureAssistantPlaceholder();
    const stream = new apiState.FakeFetchEventStream('s1');
    store.listen('s1', store.sessionContextToken, stream);

    await stream.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'attempt one partial',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-1'
    });
    expect(store.activeAttemptId).toBe('attempt-1');

    await stream.dispatch('lifecycle', {
      name: 'attempt_start',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-2'
    });
    expect(store.activeAttemptId).toBe('attempt-2');
    expect(store.messages.at(-1)?.content).toBe('');

    await stream.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'attempt two output',
      session_id: 's1',
      execution_id: executionId,
      attempt_id: 'attempt-2'
    });
    expect(store.messages.at(-1)?.content).toBe('attempt two output');
    expect(store.messages.at(-1)?.content).not.toContain('attempt one partial');
  });

  it('ignores diagnostic marker events for frontend running state', async () => {
    setSession('s1', {
      latest_execution_status: 'idle',
      messages: [
        {
          id: 'm1',
          role: 'user',
          message_type: 'text',
          content: 'State should not be affected',
          created_at: '2026-01-01T00:00:00.000Z'
        }
      ]
    });

    const store = initStore();
    store.sessionId = 's1';
    store.sessions = [
      {
        session_id: 's1',
        title: 'Session 1',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_execution_status: 'idle',
        message_count: 1
      }
    ];

    await store.submit('Trigger diagnostics');
    const stream = apiState.streamBySession.get('s1');
    const executionId = apiState.sessionState.get('s1')?.latest_execution?.id;
    await stream?.dispatch('lifecycle', {
      name: 'run_start',
      content: 'run_start',
      session_id: 's1',
      execution_id: executionId
    });
    expect(store.runLifecycle).toBe('running');

    await stream?.dispatch('messages', {
      name: 'agent_runtime_marker',
      content: 'llm_total_inference_ms',
      session_id: 's1',
      execution_id: executionId,
      value: 120
    });

    expect(store.runLifecycle).toBe('running');
    const streamingMessage = store.messages.find(
      (msg) => msg.assistant_state === 'streaming' || msg.assistant_state === 'pending'
    );
    expect(Boolean(streamingMessage)).toBe(true);
  });

  it('does not let old session SSE events pollute the active session after switching', async () => {
    setSession('s1', {
      latest_execution_status: 'idle',
      messages: [
        {
          id: 'm1',
          role: 'user',
          message_type: 'text',
          content: 'Session one',
          created_at: '2026-01-01T00:00:00.000Z'
        }
      ]
    });
    setSession('s2', {
      latest_execution_status: 'idle',
      messages: [
        {
          id: 'm2',
          role: 'user',
          message_type: 'text',
          content: 'Session two history',
          created_at: '2026-01-01T00:00:00.000Z'
        }
      ]
    });

    const store = initStore();
    store.sessionId = 's1';
    store.sessions = [
      {
        session_id: 's1',
        title: 'Session 1',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_execution_status: 'idle',
        message_count: 1
      },
      {
        session_id: 's2',
        title: 'Session 2',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_execution_status: 'idle',
        message_count: 1
      }
    ];

    await store.submit('Please start generating');
    const oldStream = apiState.streamBySession.get('s1');
    expect(store.messages.some((msg) => msg.assistant_state === 'pending')).toBe(true);

    await store.loadSession('s2');

    expect(store.sessionId).toBe('s2');
    expect(store.assistantStreamingState).toBe('normal');
    expect(store.messages.some((msg) => msg.assistant_state !== 'normal')).toBe(false);
    expect(oldStream?.closed).toBe(true);

    await oldStream?.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'polluted text',
      session_id: 's1',
      execution_id: 'exe-1',
      chunk: 'polluted text'
    });

    expect(store.messages.length).toBe(1);
    expect(store.messages[0].content).toBe('Session two history');
    expect(store.runLifecycle).toBe('idle');
  });

  it('rejects V3 stream envelopes that belong to another execution', async () => {
    setSession('s1', { latest_execution_status: 'idle', messages: [] });

    const store = initStore();
    store.sessionId = 's1';
    store.sessions = [
      {
        session_id: 's1',
        title: 'Session 1',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_execution_status: 'idle',
        message_count: 0
      }
    ];

    await store.submit('Please start generating');
    const stream = apiState.streamBySession.get('s1');
    api.runStatus = async () => apiState.sessionState.get('s1')?.latest_execution;
    await stream?.dispatch('messages', {
      schema_version: 3,
      execution_id: 'exe-other',
      sequence: 1,
      event_id: 'exe-other:1',
      channel: 'messages',
      namespace: [],
      attempt_id: null,
      message_id: null,
      tool_call_id: null,
      timestamp: '2026-01-01T00:00:00.000Z',
      data: {
        name: 'assistant_message_delta',
        content: 'wrong session',
        chunk: 'wrong session'
      }
    });

    expect(store.messages.some((msg) => msg.content === 'wrong session')).toBe(false);
    expect(store.messages.some((msg) => msg.assistant_state === 'pending')).toBe(true);
    expect(stream?.closed).toBe(true);
    expect(store.lastEventSequence).toBe(0);
    store.dispose();
  });

  it('startNewSession clears pending context and resets running state', async () => {
    setSession('s1', { latest_execution_status: 'idle', messages: [] });

    const store = initStore();
    store.sessionId = 's1';
    store.executionId = 'exe-foo';
    store.assistantStreamingState = 'streaming';
    store.messages = [
      { role: 'assistant', content: 'pending', message_type: 'text', assistant_state: 'streaming' }
    ];
    store.runLifecycle = 'running';

    await store.startNewSession();

    expect(store.sessionId).toBe('session-new');
    expect(store.executionId).toBe('');
    expect(store.assistantStreamingState).toBe('normal');
    expect(store.runLifecycle).toBe('idle');
    expect(store.messages.length).toBe(0);
  });

  it('failure events clear placeholders and mark the run as failed', async () => {
    setSession('s1', {
      latest_execution_status: 'running',
      messages: [],
      latest_execution: {
        id: 'exe-1',
        session_id: 's1',
        status: 'running',
        error: ''
      }
    });

    const store = initStore();
    store.sessionId = 's1';
    store.sessions = [
      {
        session_id: 's1',
        title: 'Session 1',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_execution_status: 'running',
        message_count: 0
      }
    ];

    await store.submit('Failure scenario');
    const stream = apiState.streamBySession.get('s1');
    const executionId = apiState.sessionState.get('s1')?.latest_execution?.id;

    await stream?.dispatch('lifecycle', {
      name: 'run_start',
      content: 'run_start',
      session_id: 's1',
      execution_id: executionId
    });
    await stream?.dispatch('messages', {
      name: 'assistant_message_delta',
      content: 'Partial content',
      session_id: 's1',
      execution_id: executionId,
      chunk: 'Partial content'
    });
    setSession('s1', {
      ...apiState.sessionState.get('s1'),
      latest_execution_status: 'failed',
      latest_execution: {
        ...apiState.sessionState.get('s1').latest_execution,
        id: executionId,
        status: 'failed',
        error: 'Model service unavailable'
      }
    });

    await stream?.dispatch('errors', {
      name: 'run_error',
      code: 'MODEL_SERVICE_UNAVAILABLE',
      message: 'Model service unavailable',
      session_id: 's1',
      execution_id: executionId
    });
    await settleAsyncActions();

    expect(store.runLifecycle).toBe('failed');
    expect(store.messages.some((msg) => msg.assistant_state !== 'normal')).toBe(false);
    expect(store.error).toContain('Model service unavailable');
    expect(store.error).toContain(`execution_id: ${executionId}`);
  });

  it('stops a run, closes the stream, and keeps already rendered content', async () => {
    setSession('s1', {
      latest_execution_status: 'running',
      latest_execution: {
        id: 'exe-1',
        session_id: 's1',
        agent_id: 'acc-1',
        status: 'running',
        error: ''
      }
    });
    const store = initStore();
    store.sessionId = 's1';
    store.executionId = 'exe-1';
    store.runLifecycle = 'running';
    store.messages = [
      { role: 'assistant', content: 'Already rendered', message_type: 'markdown', assistant_state: 'streaming' }
    ];
    const stream = new apiState.FakeFetchEventStream('s1');
    store.eventSource = stream;

    await store.cancelActiveRun();

    expect(stream.closed).toBe(true);
    expect(store.runLifecycle).toBe('cancelled');
    expect(store.messages[0]?.content).toBe('Already rendered');
    expect(store.messages[0]?.assistant_state).toBe('normal');
  });

  it('blocks submission while an Agent switch is still resolving', async () => {
    setSession('s1');
    let sendCount = 0;
    api.sendMessage = async () => {
      sendCount += 1;
      throw new Error('should not be called');
    };
    const store = initStore();
    store.sessionId = 's1';
    store.sessionInfo = apiState.sessionState.get('s1');
    store.isSwitchingAgent = true;

    expect(store.canSubmit).toBe(false);
    expect(await store.submit('must wait')).toBe(false);
    expect(sendCount).toBe(0);
  });

  it('recovers SESSION_AGENT_MISMATCH without keeping the optimistic message', async () => {
    setSession('s1');
    api.sendMessage = async () => {
      throw new ApiError(409, {
        code: 'SESSION_AGENT_MISMATCH',
        message: 'Session belongs to another Agent',
        retryable: false
      });
    };
    const store = initStore();
    store.sessionId = 's1';
    store.sessionInfo = apiState.sessionState.get('s1');
    let recovery = null;
    store.chooseAgent = async (agentId, force) => {
      recovery = { agentId, force };
      return true;
    };

    expect(await store.submit('do not duplicate')).toBe(false);
    expect(store.messages.some((message) => message.content === 'do not duplicate')).toBe(false);
    expect(store.lastErrorCode).toBe('SESSION_AGENT_MISMATCH');
    expect(recovery).toEqual({ agentId: 'acc-1', force: true });
  });

  it('restores a persisted waiting-input execution after refresh', async () => {
    setSession('waiting', {
      latest_execution_status: 'waiting_input',
      latest_execution: {
        id: 'exe-waiting',
        session_id: 'waiting',
        status: 'waiting_input',
        interrupt: {
          interrupt_id: 'interrupt-waiting',
          actions: [{ tool_name: 'publish', purpose: 'Confirm publication' }]
        }
      }
    });
    const store = initStore();

    expect(await store.loadSession('waiting')).toBe(true);
    expect(store.runLifecycle).toBe('waiting_input');
    expect(store.canResume).toBe(true);
    expect(store.executionId).toBe('exe-waiting');
  });

  it('keeps an active session when backend rejects deletion', async () => {
    setSession('s1', { latest_execution_status: 'running' });
    api.deleteSession = async () => {
      throw new ApiError(409, {
        code: 'SESSION_HAS_ACTIVE_EXECUTION',
        message: 'Active execution exists',
        retryable: false
      });
    };
    const store = initStore();
    store.sessionId = 's1';
    store.runLifecycle = 'running';
    store.sessions = [{
      session_id: 's1', agent_id: 'acc-1', title: 'Session 1', created_at: '', updated_at: '',
      latest_execution_status: 'running', message_count: 0
    }];

    await store.deleteSession('s1');
    expect(store.lastErrorCode).toBe('SESSION_HAS_ACTIVE_EXECUTION');
    expect(store.error).toContain('正在运行');
    expect(store.sessionId).toBe('s1');
  });
});
