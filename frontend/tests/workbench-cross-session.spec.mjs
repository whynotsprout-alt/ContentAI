import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { api } from '../src/services/api';
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

    async dispatch(name, payload, lastEventId = `id-${++this.eventCounter}`) {
      const event = {
        data: JSON.stringify(payload),
        lastEventId
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
      accounts: async () => [
        {
          id: 'acc-1',
          name: 'demo',
          positioning: '',
          topic_scoring_prompt: '',
          content_creation_prompt: '',
          hotspot_sources: []
        }
      ],
      account: async () => ({
        id: 'acc-1',
        name: 'demo',
        positioning: '',
        topic_scoring_prompt: '',
        content_creation_prompt: '',
        hotspot_sources: []
      }),
      createAccount: async (payload) => ({ id: 'acc-1', ...payload }),
      updateAccount: async (id, payload) => ({ id, ...payload }),
      deleteAccount: async () => undefined,
      sessions: async () => {
        return Array.from(apiState.sessionState.values()).map((session) => ({
          session_id: session.session_id,
          account_id: session.account_id,
          title: session.title,
          created_at: session.created_at,
          updated_at: session.updated_at,
          latest_status: session.latest_status,
          message_count: session.messages.length
        }));
      },
      session: async (sessionId) => apiState.sessionState.get(sessionId),
      createSession: async () => ({
        session_id: 'session-new',
        account_id: 'acc-1',
        title: 'new'
      }),
      deleteSession: async () => undefined,
      sendMessageStream: (sessionId) => {
        const executionId = apiState.nextExecutionId();
        const session = apiState.sessionState.get(sessionId);
        if (session) {
          session.latest_execution = {
            id: executionId,
            invocation_id: `inv-${executionId}`,
            session_id: sessionId,
            account_id: 'acc-1',
            user_message: 'Question',
            status: 'running',
            error: ''
          };
          session.latest_status = 'running';
        }
        const stream = new apiState.FakeFetchEventStream(sessionId);
        apiState.streamBySession.set(sessionId, stream);
        return stream;
      },
      resumeSessionStream: (sessionId) => {
        const stream = new apiState.FakeFetchEventStream(sessionId);
        apiState.streamBySession.set(sessionId, stream);
        return stream;
      },
      cancelSession: async () => ({ latest_status: 'cancelled' })
  });
}

function initStore() {
  setActivePinia(createPinia());
  const store = useWorkbenchStore();
  store.accountId = 'acc-1';
  return store;
}

function setSession(sessionId, patch = {}) {
  apiState.sessionState.set(sessionId, {
    session_id: sessionId,
    account_id: 'acc-1',
    title: `session-${sessionId}`,
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    latest_status: 'idle',
    messages: [],
    latest_execution: {
      id: 'exe-0',
      invocation_id: 'inv-0',
      session_id: sessionId,
      account_id: 'acc-1',
      user_message_id: '',
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
  it('shows pending, streams tokens, and writes the final assistant content', async () => {
    setSession('s1', {
      latest_status: 'idle',
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
        latest_status: 'idle',
        message_count: 1
      }
    ];

    await store.submit('Please start generating');

    const pending = store.messages[store.messages.length - 1];
    expect(store.runLifecycle).toBe('running');
    expect(pending?.role).toBe('assistant');
    expect(pending?.assistant_state).toBe('pending');

    const stream = apiState.streamBySession.get('s1');
    const executionId = apiState.sessionState.get('s1')?.latest_execution?.id;
    expect(stream?.closed).toBe(false);

    await stream?.dispatch('token', {
      type: 'token',
      name: 'execution_started',
      content: 'execution_started',
      session_id: 's1',
      execution_id: executionId
    });
    expect(store.runLifecycle).toBe('running');
    expect(store.assistantStreamingState).toBe('streaming');

    await stream?.dispatch('token', {
      type: 'token',
      name: 'assistant_message_delta',
      content: 'Hello',
      session_id: 's1',
      execution_id: executionId,
      chunk: 'Hello'
    });
    await stream?.dispatch('token', {
      type: 'token',
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
      latest_status: 'completed',
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

    await stream?.dispatch('token', {
      type: 'token',
      name: 'assistant_message',
      session_id: 's1',
      execution_id: executionId,
      content: 'Hello world'
    });
    await stream?.dispatch('token', {
      type: 'token',
      name: 'execution_completed',
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
      latest_status: 'idle',
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
        latest_status: 'idle',
        message_count: 1
      }
    ];

    await store.submit('Trigger diagnostics');
    const stream = apiState.streamBySession.get('s1');
    const executionId = apiState.sessionState.get('s1')?.latest_execution?.id;
    await stream?.dispatch('token', {
      type: 'token',
      name: 'execution_started',
      content: 'execution_started',
      session_id: 's1',
      execution_id: executionId
    });
    expect(store.runLifecycle).toBe('running');

    await stream?.dispatch('token', {
      type: 'token',
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
      latest_status: 'idle',
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
      latest_status: 'idle',
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
        latest_status: 'idle',
        message_count: 1
      },
      {
        session_id: 's2',
        title: 'Session 2',
        created_at: '2026-01-01T00:00:00.000Z',
        updated_at: '2026-01-01T00:00:00.000Z',
        latest_status: 'idle',
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

    await oldStream?.dispatch('token', {
      type: 'token',
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

  it('startNewSession clears pending context and resets running state', async () => {
    setSession('s1', { latest_status: 'idle', messages: [] });

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
      latest_status: 'running',
      messages: [],
      latest_execution: {
        id: 'exe-1',
        invocation_id: 'inv-1',
        session_id: 's1',
        account_id: 'acc-1',
        user_message_id: '',
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
        latest_status: 'running',
        message_count: 0
      }
    ];

    await store.submit('Failure scenario');
    const stream = apiState.streamBySession.get('s1');
    const executionId = apiState.sessionState.get('s1')?.latest_execution?.id;

    await stream?.dispatch('token', {
      type: 'token',
      name: 'execution_started',
      content: 'execution_started',
      session_id: 's1',
      execution_id: executionId
    });
    await stream?.dispatch('token', {
      type: 'token',
      name: 'assistant_message_delta',
      content: 'Partial content',
      session_id: 's1',
      execution_id: executionId,
      chunk: 'Partial content'
    });
    setSession('s1', {
      ...apiState.sessionState.get('s1'),
      latest_status: 'failed',
      latest_execution: {
        ...apiState.sessionState.get('s1').latest_execution,
        id: executionId,
        status: 'failed',
        error: 'Model service unavailable'
      }
    });

    await stream?.dispatch('token', {
      type: 'token',
      name: 'execution_failed',
      content: 'execution_failed: Model service unavailable',
      session_id: 's1',
      execution_id: executionId
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(store.runLifecycle).toBe('failed');
    expect(store.messages.some((msg) => msg.assistant_state !== 'normal')).toBe(false);
    expect(store.error).toBe('Model service unavailable');
  });
});
