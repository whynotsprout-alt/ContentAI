import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/shared/services/api';

function okResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('chat API request contracts', () => {
  it('requests the paginated session envelope with the supported filters', async () => {
    const fetchMock = vi.fn(async () => okResponse({ items: [], next_cursor: null }));
    vi.stubGlobal('document', { cookie: '' });
    vi.stubGlobal('fetch', fetchMock);

    await api.sessions({ agent_id: 'agent-1', cursor: 'cursor-1', limit: 200 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/chat/sessions?agent_id=agent-1&cursor=cursor-1&limit=200'
    );
  });

  it('requests a session message page with cursor and limit', async () => {
    const fetchMock = vi.fn(async () => okResponse({
      session_id: 'session-1',
      messages: [],
      next_cursor: null,
      latest_execution: null
    }));
    vi.stubGlobal('document', { cookie: '' });
    vi.stubGlobal('fetch', fetchMock);

    await api.session('session-1', { cursor: 'message-cursor-1', limit: 200 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/chat/sessions/session-1?cursor=message-cursor-1&limit=200'
    );
  });

  it('does not send a client-selected agent id with a session message', async () => {
    const fetchMock = vi.fn(async () => okResponse({
      session_id: 'session-1',
      message_id: 'message-1',
      execution_id: 'execution-1',
      status: 'pending'
    }));
    vi.stubGlobal('document', { cookie: '' });
    vi.stubGlobal('fetch', fetchMock);

    await api.sendMessage('session-1', {
      message: 'hello',
      idempotency_key: 'key-1'
    });

    const init = fetchMock.mock.calls[0][1];
    expect(JSON.parse(init.body)).toEqual({ message: 'hello', idempotency_key: 'key-1' });
    expect(init.headers).toMatchObject({ 'Idempotency-Key': 'key-1' });
  });

  it('uses the interrupt decision contract when resuming an execution', async () => {
    const fetchMock = vi.fn(async () => okResponse({
      id: 'execution-1',
      session_id: 'session-1',
      status: 'running',
      interrupt: null
    }));
    vi.stubGlobal('document', { cookie: '' });
    vi.stubGlobal('fetch', fetchMock);

    await api.resumeRun('execution-1', {
      interrupt_id: 'interrupt-1',
      decision: 'approve'
    });

    const init = fetchMock.mock.calls[0][1];
    expect(JSON.parse(init.body)).toEqual({
      interrupt_id: 'interrupt-1',
      decision: 'approve'
    });
  });
});
