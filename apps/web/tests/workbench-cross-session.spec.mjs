import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { ApiError, FetchEventStream, api } from '../src/services/api';
import { useWorkbenchStore } from '../src/stores/workbench';

const apiState = (() => {
  const sessionState = new Map();
  const streamBySession = new Map();
  let executionCounter = 0;

  class FakeFetchEventStream {
    listeners = new Map();
    onerror = null;
    closed = false;
    eventCounter = 0;

    constructor(sessionId) {
      this.sessionId = sessionId;
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
      const data = { ...(payload ?? {}) };
      delete data.execution_id;
      delete data.session_id;
      delete data.thread_id;
      delete data.type;
      const envelope = payload && typeof payload === 'object' && payload.schema_version === 3
        ? payload
        : {
            schema_version: 3,
            execution_id: executionId,
            sequence,
            event_id: `${executionId}:${sequence}`,
            channel: name,
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
      agents: async () => [
        {
          id: 'acc-1',
          name: 'demo',
          positioning: '',
          topic_scoring_prompt: '',
          content_creation_prompt: '',
          hotspot_sources: []
        }
      ],
      agent: async () => ({
        id: 'acc-1',
        name: 'demo',
        positioning: '',
        topic_scoring_prompt: '',
        content_creation_prompt: '',
        hotspot_sources: []
      }),
      createAgent: async (payload) => ({ id: 'acc-1', ...payload }),
      updateAgent: async (id, payload) => ({ id, ...payload }),
      deleteAgent: async () => undefined,
      sessions: async () => {
        return Array.from(apiState.sessionState.values()).map((session) => ({
          session_id: session.session_id,
          agent_id: session.agent_id,
          title: session.title,
          created_at: session.created_at,
          updated_at: session.updated_at,
          latest_execution_status: session.latest_execution_status,
          message_count: session.messages.length
        }));
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
      sendMessage: async (sessionId) => {
        const executionId = apiState.nextExecutionId();
        const session = apiState.sessionState.get(sessionId);
        if (session) {
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
          message_id: `msg-${executionId}`,
          execution_id: executionId,
          status: 'pending'
        };
      },
      executionEvents: (executionId) => {
        const session = Array.from(apiState.sessionState.values()).find(
          (item) => item.latest_execution?.id === executionId
        );
        const sessionId = session?.session_id ?? '';
        const stream = new apiState.FakeFetchEventStream(sessionId);
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
    latest_execution: {
      id: 'exe-0',
      session_id: sessionId,
      status: 'idle',
      error: ''
    },
    ...patch
  });
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
    await Promise.resolve();
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
          created_at: '2026-01-01T00:00:00.000Z'
        }
      ]
    });

    await stream?.dispatch('messages', {
      name: 'assistant_message',
      session_id: 's1',
      execution_id: executionId,
      content: 'Hello world'
    });
    await stream?.dispatch('lifecycle', {
      name: 'run_finish',
      session_id: 's1',
      execution_id: executionId
    });

    await Promise.resolve();

    expect(store.runLifecycle).toBe('completed');
    const last = store.messages[store.messages.length - 1];
    expect(last.role).toBe('assistant');
    expect(last.content).toBe('Hello world');
    expect(last.assistant_state).toBe('normal');
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

  it('ignores V3 stream envelopes that belong to another execution', async () => {
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
    await stream?.dispatch('messages', {
      schema_version: 3,
      execution_id: 'exe-other',
      sequence: 1,
      event_id: 'exe-other:1',
      channel: 'messages',
      data: {
        name: 'assistant_message_delta',
        content: 'wrong session',
        chunk: 'wrong session'
      }
    });

    expect(store.messages.some((msg) => msg.content === 'wrong session')).toBe(false);
    expect(store.messages.some((msg) => msg.assistant_state === 'pending')).toBe(true);
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
      message: 'Model service unavailable',
      session_id: 's1',
      execution_id: executionId
    });
    await Promise.resolve();
    await Promise.resolve();

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
        agent_id: 'acc-1',
        status: 'waiting_input',
        interrupt_payload: { interrupts: [{ value: 'Confirm publication' }] }
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
