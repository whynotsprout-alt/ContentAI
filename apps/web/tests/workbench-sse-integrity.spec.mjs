import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { api } from '../src/shared/services/api';
import { useWorkbenchStore } from '../src/features/workbench/stores/workbench.store';

class FakeFetchEventStream {
  listeners = new Map();
  onerror = null;
  closed = false;

  addEventListener(name, listener) {
    const handlers = this.listeners.get(name) ?? [];
    handlers.push(listener);
    this.listeners.set(name, handlers);
  }

  close() {
    this.closed = true;
    this.listeners.clear();
  }

  async emitRaw(channel, data, lastEventId = '', transportType = channel) {
    const handlers = [...(this.listeners.get(channel) ?? [])];
    const event = { type: transportType, data, lastEventId };
    for (const handler of handlers) await handler(event);
  }

  async emit(channel, envelope, lastEventId = envelope.event_id ?? '') {
    await this.emitRaw(channel, JSON.stringify(envelope), lastEventId);
  }

  async emitSignal(name) {
    const handlers = [...(this.listeners.get(name) ?? [])];
    for (const handler of handlers) await handler({ type: name, data: '', lastEventId: '' });
  }
}

const executionId = 'exe-integrity';
let persistedSession;
let runStatusCalls;
let executionEventCalls;

function execution(status = 'running', id = executionId) {
  return {
    id,
    session_id: 's1',
    status,
    error: null,
    interrupt: null
  };
}

function session(latestExecution = execution()) {
  return {
    session_id: 's1',
    agent_id: 'acc-1',
    title: 'SSE integrity',
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    latest_execution_status: latestExecution?.status ?? 'idle',
    latest_execution: latestExecution,
    messages: [],
    next_cursor: null
  };
}

function frame(sequence, channel, data, id = executionId) {
  return {
    schema_version: 3,
    execution_id: id,
    sequence,
    event_id: `${id}:${sequence}`,
    channel,
    namespace: [],
    attempt_id: 'att-1',
    message_id: null,
    tool_call_id: null,
    timestamp: '2026-01-01T00:00:00.000Z',
    data
  };
}

function startListening({ knownCursor = true } = {}) {
  const store = useWorkbenchStore();
  store.agentId = 'acc-1';
  store.sessionId = 's1';
  store.sessionInfo = persistedSession;
  store.executionInfo = persistedSession.latest_execution;
  store._setExecutionScope(executionId, knownCursor);
  store.runLifecycle = 'queued';
  const source = new FakeFetchEventStream();
  store.listen('s1', store.sessionContextToken, source);
  return { source, store };
}

async function flushAsyncActions() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

beforeEach(() => {
  if (typeof globalThis.window === 'undefined') globalThis.window = globalThis;
  if (!globalThis.window.setTimeout) {
    globalThis.window.setTimeout = setTimeout;
    globalThis.window.clearTimeout = clearTimeout;
  }
  setActivePinia(createPinia());
  persistedSession = session();
  runStatusCalls = [];
  executionEventCalls = [];
  Object.assign(api, {
    session: async () => persistedSession,
    sessions: async () => ({ items: [], next_cursor: null }),
    runStatus: async (id) => {
      runStatusCalls.push(id);
      return execution('running', id);
    },
    executionEvents: (id, afterSequence = 0) => {
      executionEventCalls.push({ id, afterSequence });
      return new FakeFetchEventStream();
    },
    resumeRun: async (id) => execution('running', id),
    cancelRun: async (id) => execution('cancelled', id),
    sendMessage: async () => ({
      session_id: 's1',
      message_id: 'message-default',
      execution_id: executionId,
      status: 'pending'
    })
  });
});

describe('workbench V3 SSE integrity', () => {
  const invalidFrames = [
    {
      name: 'malformed JSON',
      channel: 'messages',
      data: '{',
      lastEventId: `${executionId}:1`
    },
    {
      name: 'a forged envelope id',
      channel: 'messages',
      data: JSON.stringify({
        ...frame(1, 'messages', { name: 'assistant_message_delta', content: 'forged' }),
        event_id: `${executionId}:999`
      }),
      lastEventId: `${executionId}:999`
    },
    {
      name: 'another execution',
      channel: 'messages',
      data: JSON.stringify(frame(
        1,
        'messages',
        { name: 'assistant_message_delta', content: 'wrong execution' },
        'exe-other'
      )),
      lastEventId: 'exe-other:1'
    },
    {
      name: 'a transport/envelope id mismatch',
      channel: 'messages',
      data: JSON.stringify(frame(
        1,
        'messages',
        { name: 'assistant_message_delta', content: 'mismatched id' }
      )),
      lastEventId: `${executionId}:2`
    },
    {
      name: 'a sequence gap',
      channel: 'messages',
      data: JSON.stringify(frame(
        2,
        'messages',
        { name: 'assistant_message_delta', content: 'gap' }
      )),
      lastEventId: `${executionId}:2`
    },
    {
      name: 'a transport/envelope channel mismatch',
      channel: 'messages',
      data: JSON.stringify(frame(1, 'tools', { name: 'tool_progress' })),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'a timestamp without a timezone',
      channel: 'values',
      data: JSON.stringify({
        ...frame(1, 'values', { name: 'progress' }),
        timestamp: '2026-01-01T00:00:00'
      }),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'a non-string namespace item',
      channel: 'values',
      data: JSON.stringify({
        ...frame(1, 'values', { name: 'progress' }),
        namespace: [1]
      }),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'a non-nullable attempt id',
      channel: 'values',
      data: JSON.stringify({
        ...frame(1, 'values', { name: 'progress' }),
        attempt_id: 42
      }),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'numeric assistant delta content',
      channel: 'messages',
      data: JSON.stringify(frame(1, 'messages', {
        name: 'assistant_message_delta',
        content: 42,
        chunk: 7,
        done: false
      })),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'a malformed sequenced error',
      channel: 'errors',
      data: JSON.stringify(frame(1, 'errors', {
        name: 'run_error',
        code: '',
        message: 'failure'
      })),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'a malformed public interrupt',
      channel: 'interrupts',
      data: JSON.stringify(frame(1, 'interrupts', {
        name: 'run_interrupt',
        interrupt: { interrupt_id: '', actions: 'invalid' }
      })),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'an interrupt without an event name',
      channel: 'interrupts',
      data: JSON.stringify(frame(1, 'interrupts', {
        interrupt: {
          interrupt_id: 'interrupt-missing-name',
          actions: [{ tool_name: 'publish', purpose: 'Confirm publication' }]
        }
      })),
      lastEventId: `${executionId}:1`
    },
    {
      name: 'an interrupt with an unknown event name',
      channel: 'interrupts',
      data: JSON.stringify(frame(1, 'interrupts', {
        name: 'private_interrupt',
        interrupt: {
          interrupt_id: 'interrupt-unknown-name',
          actions: [{ tool_name: 'publish', purpose: 'Confirm publication' }]
        }
      })),
      lastEventId: `${executionId}:1`
    }
  ];

  it.each(invalidFrames)('rejects $name without advancing the cursor', async (testCase) => {
    const { source, store } = startListening();

    await source.emitRaw(testCase.channel, testCase.data, testCase.lastEventId);
    await flushAsyncActions();

    expect(source.closed).toBe(true);
    expect(store.lastEventSequence).toBe(0);
    expect(store.processedSseEventIds).toEqual([]);
    expect(store.messages).toEqual([]);
    expect(runStatusCalls).toEqual([executionId]);
    store.dispose();
  });

  it('accepts contiguous V3 frames and applies their side effects once', async () => {
    const { source, store } = startListening();

    await source.emit('lifecycle', frame(1, 'lifecycle', { name: 'run_start' }));
    await source.emit('messages', frame(2, 'messages', {
      name: 'assistant_message_delta',
      content: '连续',
      chunk: '连续',
      message_type: 'markdown',
      done: false
    }));

    expect(source.closed).toBe(false);
    expect(store.lastEventSequence).toBe(2);
    expect(store.cursorKnown).toBe(true);
    expect(store.eventCursorExecutionId).toBe(executionId);
    expect(store.messages.at(-1)?.content).toBe('连续');
    store.dispose();
  });

  it('ignores an exact replay of an already confirmed frame', async () => {
    const { source, store } = startListening();
    const envelope = frame(1, 'messages', {
      name: 'assistant_message_delta',
      content: 'once',
      chunk: 'once',
      message_type: 'markdown',
      done: false
    });

    await source.emit('messages', envelope);
    await source.emit('messages', envelope);

    expect(source.closed).toBe(false);
    expect(store.lastEventSequence).toBe(1);
    expect(store.messages.at(-1)?.content).toBe('once');
    expect(store.processedSseEventIds).toHaveLength(1);
    store.dispose();
    expect(store.processedSseEventIds).toEqual([]);
    expect(store.processedSseEventCharacterCount).toBe(0);
  });

  it('accepts MAX_SAFE_INTEGER only when contiguous and rejects an unsafe sequence', async () => {
    const maxSafe = Number.MAX_SAFE_INTEGER;
    const first = startListening();
    first.store.lastEventSequence = maxSafe - 1;

    await first.source.emit('values', frame(maxSafe, 'values', { name: 'max-safe' }));

    expect(first.source.closed).toBe(false);
    expect(first.store.lastEventSequence).toBe(maxSafe);
    first.store.dispose();

    const unsafe = startListening();
    await unsafe.source.emit(
      'values',
      frame(maxSafe + 1, 'values', { name: 'unsafe' })
    );

    expect(unsafe.source.closed).toBe(true);
    expect(unsafe.store.lastEventSequence).toBe(0);
    unsafe.store.dispose();
  });

  it('bounds byte-exact replay fingerprints with FIFO eviction', () => {
    const store = useWorkbenchStore();
    store._setExecutionScope(executionId, true);
    const largeValue = 'x'.repeat(1_100_000);
    const firstEnvelope = frame(1, 'values', { name: 'large-1', value: largeValue });
    const secondEnvelope = frame(2, 'values', { name: 'large-2', value: largeValue });
    const eventFor = (envelope) => ({
      type: 'values',
      data: JSON.stringify(envelope),
      lastEventId: envelope.event_id
    });

    expect(store._consumeSseFrame(eventFor(firstEnvelope), executionId).kind).toBe('accepted');
    expect(store._consumeSseFrame(eventFor(secondEnvelope), executionId).kind).toBe('accepted');

    expect(store.processedSseEventIds).toHaveLength(1);
    expect(store.processedSseEventCharacterCount).toBeLessThanOrEqual(2 * 1024 * 1024);
    expect(store._consumeSseFrame(eventFor(firstEnvelope), executionId).kind).toBe('invalid');

    store._setExecutionScope('exe-next');
    expect(store.processedSseEventIds).toEqual([]);
    expect(store.processedSseEventCharacterCount).toBe(0);
  });

  it('does not let nested reserved metadata bypass the attempt fence', async () => {
    const { source, store } = startListening();
    store.activeAttemptId = 'att-current';
    const envelope = {
      ...frame(1, 'messages', {
        name: 'assistant_message_delta',
        content: 'must not render',
        chunk: 'must not render',
        attempt_id: 'att-current',
        execution_id: executionId,
        event_id: `${executionId}:1`
      }),
      attempt_id: 'att-stale'
    };

    await source.emit('messages', envelope);

    expect(store.lastEventSequence).toBe(1);
    expect(store.activeAttemptId).toBe('att-current');
    expect(store.messages.some((message) => message.content === 'must not render')).toBe(false);
    store.dispose();
  });

  it('treats an unsequenced STREAM_EXCEPTION_ERROR as stream degradation', async () => {
    const { source, store } = startListening();
    await source.emit('values', frame(1, 'values', { name: 'progress' }));
    store.activeAttemptId = 'att-current';
    const confirmedFrames = [...store.processedSseEventIds];

    await source.emit('errors', {
      schema_version: 3,
      execution_id: executionId,
      channel: 'errors',
      data: {
        name: 'stream_exception',
        code: 'STREAM_EXCEPTION_ERROR',
        message: 'Event stream processing failed.',
        attempt_id: 'att-forged',
        execution_id: 'exe-forged'
      }
    }, '');
    await flushAsyncActions();

    expect(source.closed).toBe(true);
    expect(store.lastEventSequence).toBe(1);
    expect(store.processedSseEventIds).toEqual(confirmedFrames);
    expect(store.activeAttemptId).toBe('att-current');
    expect(store.error).toBe('');
    expect(runStatusCalls).toEqual([executionId]);
    store.dispose();
  });

  it('resumes the same execution from its confirmed cursor', async () => {
    const { source, store } = startListening();
    await source.emit('values', frame(1, 'values', { name: 'progress' }));
    store.status = 'waiting_input';
    store.runLifecycle = 'waiting_input';
    store.executionInfo = {
      ...execution('waiting_input'),
      interrupt: { interrupt_id: 'interrupt-1', actions: [] }
    };

    expect(await store.resume('approve')).toBe(true);

    expect(store.lastEventSequence).toBe(1);
    expect(store.cursorKnown).toBe(true);
    expect(store.eventCursorExecutionId).toBe(executionId);
    expect(executionEventCalls).toEqual([{ id: executionId, afterSequence: 1 }]);
    store.dispose();
  });

  it('clears cursor state when the execution scope changes', async () => {
    const { source, store } = startListening();
    await source.emit('values', frame(1, 'values', { name: 'progress' }));

    store._setExecutionScope('exe-next');

    expect(store.executionId).toBe('exe-next');
    expect(store.eventCursorExecutionId).toBe('exe-next');
    expect(store.lastEventSequence).toBe(0);
    expect(store.processedSseEventIds).toEqual([]);
    expect(store.cursorKnown).toBe(false);
    store.dispose();
  });

  it('polls after resuming a refreshed waiting-input execution with an unknown cursor', async () => {
    persistedSession = session({
      ...execution('waiting_input'),
      interrupt: { interrupt_id: 'interrupt-refresh', actions: [] }
    });
    const store = useWorkbenchStore();
    store.agentId = 'acc-1';

    expect(await store.loadSession('s1')).toBe(true);
    expect(store.cursorKnown).toBe(false);
    expect(store.eventCursorExecutionId).toBe(executionId);

    expect(await store.resume('approve')).toBe(true);
    await flushAsyncActions();

    expect(executionEventCalls).toEqual([]);
    expect(runStatusCalls).toEqual([executionId]);
    expect(store.lastEventSequence).toBe(0);
    expect(store.cursorKnown).toBe(false);
    store.dispose();
  });

  it('fences a late terminal snapshot after a new execution starts', async () => {
    const { source: sourceA, store } = startListening();
    const sessionRequested = deferred();
    const lateSession = deferred();
    api.session = async () => {
      sessionRequested.resolve();
      return lateSession.promise;
    };
    api.sendMessage = async () => ({
      session_id: 's1',
      message_id: 'message-b',
      execution_id: 'exe-b',
      status: 'pending'
    });
    let sourceB;
    api.executionEvents = (id, afterSequence = 0) => {
      executionEventCalls.push({ id, afterSequence });
      sourceB = new FakeFetchEventStream();
      return sourceB;
    };

    await sourceA.emit('lifecycle', frame(1, 'lifecycle', { name: 'run_finish' }));
    await sessionRequested.promise;
    expect(await store.submit('run B')).toBe(true);
    expect(store.executionId).toBe('exe-b');
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);

    lateSession.resolve(session(execution('completed')));
    await flushAsyncActions();

    expect(store.executionId).toBe('exe-b');
    expect(store.eventSource).not.toBeNull();
    expect(sourceB.closed).toBe(false);
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);

    await sourceB.emit('messages', frame(1, 'messages', {
      name: 'assistant_message_delta',
      content: 'B survives',
      chunk: 'B survives',
      done: false
    }, 'exe-b'));
    expect(store.lastEventSequence).toBe(1);
    expect(store.messages.at(-1)?.content).toBe('B survives');
    store.dispose();
  });

  it('does not let a late waiting-input refresh clear a same-execution resume source', async () => {
    const { source: waitingSource, store } = startListening();
    const waitingExecution = {
      ...execution('waiting_input'),
      interrupt: { interrupt_id: 'interrupt-late', actions: [] }
    };
    persistedSession = session(waitingExecution);
    const oldSessionsRequested = deferred();
    const oldSessions = deferred();
    let sessionsCalls = 0;
    api.sessions = async () => {
      sessionsCalls += 1;
      if (sessionsCalls === 1) {
        oldSessionsRequested.resolve();
        return oldSessions.promise;
      }
      return { items: [], next_cursor: null };
    };
    let resumedSource;
    api.executionEvents = (id, afterSequence = 0) => {
      executionEventCalls.push({ id, afterSequence });
      resumedSource = new FakeFetchEventStream();
      return resumedSource;
    };

    await waitingSource.emit('interrupts', frame(1, 'interrupts', {
      name: 'run_interrupt',
      interrupt: {
        interrupt_id: 'interrupt-late',
        actions: [{ tool_name: 'publish', purpose: 'Confirm publication' }]
      }
    }));
    await oldSessionsRequested.promise;

    expect(store.canResume).toBe(true);
    expect(await store.resume('approve')).toBe(true);
    expect(executionEventCalls).toEqual([{ id: executionId, afterSequence: 1 }]);
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);

    oldSessions.resolve({ items: [], next_cursor: null });
    await flushAsyncActions();

    expect(store.eventSource).not.toBeNull();
    expect(resumedSource.closed).toBe(false);
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);

    await resumedSource.emit('messages', frame(2, 'messages', {
      name: 'assistant_message_delta',
      content: 'resumed safely',
      chunk: 'resumed safely',
      done: false
    }));
    expect(store.lastEventSequence).toBe(2);
    expect(store.messages.at(-1)?.content).toBe('resumed safely');
    store.dispose();
  });

  it('fences an in-flight close recovery before a replacement source is installed', async () => {
    const { source: oldSource, store } = startListening();
    const statusRequested = deferred();
    const lateStatus = deferred();
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      statusRequested.resolve();
      return lateStatus.promise;
    };

    const oldRecovery = oldSource.emitSignal('close');
    await statusRequested.promise;
    const replacementSource = new FakeFetchEventStream();
    store.listen('s1', store.sessionContextToken, replacementSource);

    lateStatus.resolve(execution('running'));
    await oldRecovery;

    expect(store.eventSource).not.toBeNull();
    expect(replacementSource.closed).toBe(false);
    await replacementSource.emit('values', frame(1, 'values', { name: 'replacement' }));
    expect(store.lastEventSequence).toBe(1);
    store.dispose();
  });

  it('fences an in-flight run-status poll before a replacement source is installed', async () => {
    const { source: oldSource, store } = startListening();
    const statusRequested = deferred();
    const lateStatus = deferred();
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      statusRequested.resolve();
      return lateStatus.promise;
    };

    store._startRunStatusPolling('s1', store.sessionContextToken);
    await statusRequested.promise;

    const replacementSource = new FakeFetchEventStream();
    store.listen('s1', store.sessionContextToken, replacementSource);
    await replacementSource.emit(
      'lifecycle',
      frame(1, 'lifecycle', { name: 'run_start' })
    );

    lateStatus.resolve(execution('completed'));
    await flushAsyncActions();

    expect(oldSource.closed).toBe(true);
    expect(store.eventSource).not.toBeNull();
    expect(replacementSource.closed).toBe(false);
    expect(store.runLifecycle).toBe('running');
    expect(store.lastEventSequence).toBe(1);

    await replacementSource.emit('values', frame(2, 'values', { name: 'replacement' }));
    expect(store.lastEventSequence).toBe(2);
    store.dispose();
  });

  it('lets only the newest same-generation run-status poll commit', async () => {
    const { store } = startListening();
    const firstRequested = deferred();
    const secondRequested = deferred();
    const firstStatus = deferred();
    const secondStatus = deferred();
    let statusCalls = 0;
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      statusCalls += 1;
      if (statusCalls === 1) {
        firstRequested.resolve();
        return firstStatus.promise;
      }
      secondRequested.resolve();
      return secondStatus.promise;
    };

    store._startRunStatusPolling('s1', store.sessionContextToken);
    await firstRequested.promise;
    store._startRunStatusPolling('s1', store.sessionContextToken);
    await secondRequested.promise;

    secondStatus.resolve(execution('running'));
    await flushAsyncActions();
    const newestPollTimer = store.ssePollTimer;
    expect(newestPollTimer).not.toBeNull();
    expect(store.runLifecycle).toBe('running');

    firstStatus.resolve(execution('completed'));
    await flushAsyncActions();

    expect(runStatusCalls).toEqual([executionId, executionId]);
    expect(store.runLifecycle).toBe('running');
    expect(store.ssePollTimer).toBe(newestPollTimer);
    store.dispose();
  });

  it.each([
    {
      lifecycle: 'completed',
      channel: 'lifecycle',
      data: { name: 'run_finish' }
    },
    {
      lifecycle: 'cancelled',
      channel: 'lifecycle',
      data: { name: 'run_cancel' }
    },
    {
      lifecycle: 'failed',
      channel: 'errors',
      data: { name: 'run_error', code: 'RUN_FAILED', message: 'failure' }
    }
  ])('rejects a same-lifecycle execution B snapshot after observing $lifecycle for A', async ({
    lifecycle,
    channel,
    data
  }) => {
    const { source, store } = startListening();
    const stableMessages = [{
      role: 'user',
      content: 'execution A message',
      message_type: 'text',
      assistant_state: 'normal'
    }];
    store.messages = stableMessages.map((message) => ({ ...message }));
    const executionBSnapshot = {
      ...session(execution(lifecycle, 'exe-b')),
      messages: [{
        id: 'message-b',
        role: 'assistant',
        message_type: 'text',
        content: 'must not hydrate from execution B',
        created_at: '2026-01-01T00:00:01.000Z'
      }]
    };
    api.session = async () => executionBSnapshot;
    const statusRequested = deferred();
    const lateStatus = deferred();
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      statusRequested.resolve();
      return lateStatus.promise;
    };

    await source.emit(channel, frame(1, channel, data));
    await statusRequested.promise;

    expect(source.closed).toBe(true);
    expect(store.executionId).toBe(executionId);
    expect(store.executionInfo?.id).toBe(executionId);
    expect(store.executionInfo?.session_id).toBe('s1');
    expect(store.eventCursorExecutionId).toBe(executionId);
    expect(store.lastEventSequence).toBe(1);
    expect(store.messages).toEqual(stableMessages);
    expect(store.runLifecycle).toBe(lifecycle);

    store.dispose();
    lateStatus.resolve(execution(lifecycle));
    await flushAsyncActions();
  });

  it('preserves waiting-input while a stale running snapshot converges', async () => {
    const { source, store } = startListening();
    const publicInterrupt = {
      interrupt_id: 'interrupt-stale-session',
      actions: [{ tool_name: 'publish', purpose: 'Confirm publication' }]
    };
    const waitingExecution = {
      ...execution('waiting_input'),
      interrupt: publicInterrupt
    };
    const staleSessionRequested = deferred();
    const staleSession = deferred();
    const statusRequested = deferred();
    const staleStatus = deferred();
    const finalStatusRequested = deferred();
    const finalStatus = deferred();
    let sessionCalls = 0;
    let statusCalls = 0;
    api.session = async () => {
      sessionCalls += 1;
      if (sessionCalls === 1) {
        staleSessionRequested.resolve();
        return staleSession.promise;
      }
      return session(waitingExecution);
    };
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      statusCalls += 1;
      if (statusCalls === 1) {
        statusRequested.resolve();
        return staleStatus.promise;
      }
      finalStatusRequested.resolve();
      return finalStatus.promise;
    };

    await source.emit('interrupts', frame(1, 'interrupts', {
      name: 'run_interrupt',
      interrupt: publicInterrupt
    }));
    await staleSessionRequested.promise;

    staleSession.resolve(session(execution('running')));
    await statusRequested.promise;

    expect(source.closed).toBe(true);
    expect(store.runLifecycle).toBe('waiting_input');
    expect(store.status).toBe('waiting_input');
    expect(store.lastEventSequence).toBe(1);
    expect(store.canResume).toBe(true);
    expect(store.executionInfo?.interrupt).toEqual(publicInterrupt);

    staleStatus.resolve(execution('running'));
    await flushAsyncActions();

    expect(store.runLifecycle).toBe('waiting_input');
    expect(store.canResume).toBe(true);
    expect(store.executionInfo?.interrupt).toEqual(publicInterrupt);
    expect(store.ssePollTimer).not.toBeNull();

    store._startRunStatusPolling(
      's1',
      store.sessionContextToken,
      store.streamGeneration,
      'waiting_input'
    );
    await finalStatusRequested.promise;
    finalStatus.resolve(waitingExecution);
    await flushAsyncActions();
    await flushAsyncActions();

    expect(sessionCalls).toBe(2);
    expect(runStatusCalls).toEqual([executionId, executionId]);
    expect(store.runLifecycle).toBe('waiting_input');
    expect(store.status).toBe('waiting_input');
    expect(store.canResume).toBe(true);
    expect(store.executionInfo?.interrupt).toEqual(publicInterrupt);
    expect(store.ssePollTimer).toBeNull();
    store.dispose();
  });

  it('recovers a rejected interrupt session refresh without an unhandled rejection', async () => {
    const { source, store } = startListening();
    const waitingExecution = {
      ...execution('waiting_input'),
      interrupt: { interrupt_id: 'interrupt-retry', actions: [] }
    };
    const waitingSession = session(waitingExecution);
    let sessionCalls = 0;
    api.session = async () => {
      sessionCalls += 1;
      if (sessionCalls === 1) throw new Error('transient session failure');
      return waitingSession;
    };
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      return waitingExecution;
    };

    await source.emit('interrupts', frame(1, 'interrupts', {
      name: 'run_interrupt',
      interrupt: {
        interrupt_id: 'interrupt-retry',
        actions: [{ tool_name: 'publish', purpose: 'Confirm publication' }]
      }
    }));
    await flushAsyncActions();
    await flushAsyncActions();

    expect(source.closed).toBe(true);
    expect(store.lastEventSequence).toBe(1);
    expect(runStatusCalls).toEqual([executionId]);
    expect(store.runLifecycle).toBe('waiting_input');
    expect(store.canResume).toBe(true);
    store.dispose();
  });

  it('contains a stale terminal refresh rejection after a new source exists', async () => {
    const { source: sourceA, store } = startListening();
    const sessionRequested = deferred();
    const lateSession = deferred();
    api.session = async () => {
      sessionRequested.resolve();
      return lateSession.promise;
    };

    await sourceA.emit('lifecycle', frame(1, 'lifecycle', { name: 'run_finish' }));
    await sessionRequested.promise;
    const sourceB = new FakeFetchEventStream();
    store.listen('s1', store.sessionContextToken, sourceB);
    store._ensureAssistantPlaceholder();

    lateSession.reject(new Error('late stale failure'));
    await flushAsyncActions();

    expect(store.eventSource).not.toBeNull();
    expect(sourceB.closed).toBe(false);
    expect(runStatusCalls).toEqual([]);
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);
    await sourceB.emit('values', frame(2, 'values', { name: 'still-current' }));
    expect(store.lastEventSequence).toBe(2);
    store.dispose();
  });

  it.each([
    {
      name: 'another session',
      response: {
        session_id: 'other-session',
        message_id: 'message-submit',
        execution_id: executionId,
        status: 'pending'
      }
    },
    {
      name: 'an empty message id',
      response: {
        session_id: 's1',
        message_id: '',
        execution_id: executionId,
        status: 'pending'
      }
    },
    {
      name: 'a padded execution id',
      response: {
        session_id: 's1',
        message_id: 'message-submit',
        execution_id: ` ${executionId} `,
        status: 'pending'
      }
    }
  ])('rejects a submit response containing $name', async ({ response }) => {
    persistedSession = session(null);
    const store = useWorkbenchStore();
    store.agentId = 'acc-1';
    expect(await store.loadSession('s1')).toBe(true);
    api.sendMessage = async () => response;

    expect(await store.submit('scoped submit')).toBe(false);

    expect(store.executionId).toBe('');
    expect(store.eventSource).toBeNull();
    expect(executionEventCalls).toEqual([]);
    expect(store.messages).toEqual([]);
    store.dispose();
  });

  it('fences a deferred resume response after cancel becomes the newer action', async () => {
    const waitingExecution = {
      ...execution('waiting_input'),
      interrupt: { interrupt_id: 'interrupt-resume-cancel', actions: [] }
    };
    persistedSession = session(waitingExecution);
    const store = useWorkbenchStore();
    store.agentId = 'acc-1';
    expect(await store.loadSession('s1')).toBe(true);
    const resumeRequested = deferred();
    const lateResume = deferred();
    api.resumeRun = async () => {
      resumeRequested.resolve();
      return lateResume.promise;
    };
    api.cancelRun = async () => execution('cancelled');
    api.session = async () => session(execution('cancelled'));

    const resumeResult = store.resume('approve');
    await resumeRequested.promise;
    await store.cancelActiveRun();

    lateResume.resolve(execution('running'));
    expect(await resumeResult).toBe(false);

    expect(store.executionId).toBe(executionId);
    expect(store.executionInfo?.status).toBe('cancelled');
    expect(store.runLifecycle).toBe('cancelled');
    expect(store.eventSource).toBeNull();
    expect(executionEventCalls).toEqual([]);
    store.dispose();
  });

  it('keeps a pending submit active when cancel has no verified execution id', async () => {
    persistedSession = session(null);
    const store = useWorkbenchStore();
    store.agentId = 'acc-1';
    expect(await store.loadSession('s1')).toBe(true);
    const submitRequested = deferred();
    const lateSubmit = deferred();
    api.sendMessage = async () => {
      submitRequested.resolve();
      return lateSubmit.promise;
    };
    let cancelCalls = 0;
    api.cancelRun = async () => {
      cancelCalls += 1;
      return execution('cancelled');
    };
    let installedSource;
    api.executionEvents = (id, afterSequence = 0) => {
      executionEventCalls.push({ id, afterSequence });
      installedSource = new FakeFetchEventStream();
      return installedSource;
    };

    const submitResult = store.submit('submit before cancel');
    await submitRequested.promise;
    const submitActionEpoch = store.runActionEpoch;
    await store.cancelActiveRun();

    expect(cancelCalls).toBe(0);
    expect(store.runActionEpoch).toBe(submitActionEpoch);
    expect(store.executionId).toBe('');
    expect(store.runLifecycle).toBe('queued');
    expect(store.status).toBe('pending');
    expect(store.eventSource).toBeNull();
    expect(store.messages[0]?.id).toBeUndefined();
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);

    lateSubmit.resolve({
      session_id: 's1',
      message_id: 'message-late-submit',
      execution_id: executionId,
      status: 'pending'
    });
    expect(await submitResult).toBe(true);

    expect(cancelCalls).toBe(0);
    expect(store.executionId).toBe(executionId);
    expect(store.runLifecycle).toBe('queued');
    expect(store.statusNotice).toBe('');
    expect(store.eventSource).not.toBeNull();
    expect(installedSource.closed).toBe(false);
    expect(executionEventCalls).toEqual([{ id: executionId, afterSequence: 0 }]);
    expect(store.messages[0]?.id).toBe('message-late-submit');
    expect(store.messages.some((message) => message.assistant_state === 'pending')).toBe(true);

    await installedSource.emit(
      'lifecycle',
      frame(1, 'lifecycle', { name: 'run_start' })
    );
    expect(store.runLifecycle).toBe('running');
    expect(store.lastEventSequence).toBe(1);
    expect(store.messages.some((message) => message.assistant_state === 'streaming')).toBe(true);
    store.dispose();
  });

  it('rejects execution B from a cancel session poll before mutating A scope', async () => {
    const { source, store } = startListening();
    store.messages = [{
      role: 'user',
      content: 'keep execution A',
      message_type: 'text',
      assistant_state: 'normal'
    }];
    await source.emit('values', frame(1, 'values', { name: 'execution-a-progress' }));
    api.cancelRun = async () => execution('cancelled');
    api.session = async () => ({
      ...session(execution('cancelled', 'exe-b')),
      messages: [{
        id: 'message-b',
        role: 'assistant',
        message_type: 'text',
        content: 'must not hydrate',
        created_at: '2026-01-01T00:00:01.000Z'
      }]
    });

    await store.cancelActiveRun();

    expect(store.executionId).toBe(executionId);
    expect(store.executionInfo?.id).toBe(executionId);
    expect(store.eventCursorExecutionId).toBe(executionId);
    expect(store.lastEventSequence).toBe(1);
    expect(store.messages.map((message) => message.content)).toEqual(['keep execution A']);
    store.dispose();
  });

  it('fences a deferred cancel poll after a replacement submit installs source B', async () => {
    const { store } = startListening();
    const cancelPollRequested = deferred();
    const lateCancelPoll = deferred();
    api.cancelRun = async () => execution('cancelled');
    api.session = async () => {
      cancelPollRequested.resolve();
      return lateCancelPoll.promise;
    };

    const cancelResult = store.cancelActiveRun();
    await cancelPollRequested.promise;

    store.runLifecycle = 'completed';
    store.status = 'completed';
    store.sessionInfo = session(execution('completed'));
    store.executionInfo = execution('completed');
    let replacementSource;
    api.sendMessage = async () => ({
      session_id: 's1',
      message_id: 'message-b',
      execution_id: 'exe-b',
      status: 'pending'
    });
    api.executionEvents = (id, afterSequence = 0) => {
      executionEventCalls.push({ id, afterSequence });
      replacementSource = new FakeFetchEventStream();
      return replacementSource;
    };

    expect(await store.submit('replacement execution B')).toBe(true);
    lateCancelPoll.resolve(session(execution('cancelled')));
    await cancelResult;

    expect(store.executionId).toBe('exe-b');
    expect(store.eventCursorExecutionId).toBe('exe-b');
    expect(store.eventSource).not.toBeNull();
    expect(replacementSource.closed).toBe(false);
    await replacementSource.emit(
      'values',
      frame(1, 'values', { name: 'execution-b-progress' }, 'exe-b')
    );
    expect(store.lastEventSequence).toBe(1);
    store.dispose();
  });

  it('fails closed on mismatched resume and cancel responses', async () => {
    persistedSession = session({
      ...execution('waiting_input'),
      interrupt: { interrupt_id: 'interrupt-scope', actions: [] }
    });
    const resumeStore = useWorkbenchStore();
    resumeStore.agentId = 'acc-1';
    expect(await resumeStore.loadSession('s1')).toBe(true);
    api.resumeRun = async () => execution('running', 'exe-other');

    expect(await resumeStore.resume('approve')).toBe(false);
    expect(resumeStore.executionId).toBe(executionId);
    expect(executionEventCalls).toEqual([]);
    resumeStore.dispose();

    setActivePinia(createPinia());
    persistedSession = session(execution('running'));
    const cancelStore = startListening().store;
    cancelStore.runLifecycle = 'running';
    api.cancelRun = async () => ({
      ...execution('cancelled'),
      session_id: 'other-session'
    });

    await cancelStore.cancelActiveRun();
    expect(cancelStore.executionId).toBe(executionId);
    expect(cancelStore.runLifecycle).toBe('running');
    cancelStore.dispose();
  });

  it('does not adopt a mismatched runStatus response during recovery', async () => {
    const { source, store } = startListening();
    api.runStatus = async (id) => {
      runStatusCalls.push(id);
      return { ...execution('running', id), session_id: 'other-session' };
    };

    await source.onerror?.(new Error('connection lost'));
    await flushAsyncActions();

    expect(store.executionId).toBe(executionId);
    expect(store.eventCursorExecutionId).toBe(executionId);
    expect(store.executionInfo?.session_id).toBe('s1');
    expect(store.eventSource).toBeNull();
    expect(executionEventCalls).toEqual([]);
    store.dispose();
  });
});
