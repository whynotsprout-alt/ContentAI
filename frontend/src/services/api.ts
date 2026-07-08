const configuredApiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
export const API_BASE = configuredApiBase.trim().replace(/\/$/, '');
const ACCESS_TOKEN_STORAGE_KEY = 'contentai_access_token';

let accessTokenProvider: () => string | null = () => localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);

export function setAccessToken(token: string | null) {
  if (token) {
    localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
  } else {
    localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
  }
}

export function setAccessTokenProvider(provider: () => string | null) {
  accessTokenProvider = provider;
}

export interface Account {
  id: string;
  name: string;
  positioning: string;
  topic_scoring_prompt: string;
  content_creation_prompt: string;
  hotspot_sources: string[];
}

export interface AccountPayload {
  name: string;
  positioning: string;
  topic_scoring_prompt: string;
  content_creation_prompt: string;
  hotspot_sources: string[];
}

export interface AccountUpdatePayload {
  name?: string;
  positioning?: string;
  topic_scoring_prompt?: string;
  content_creation_prompt?: string;
  hotspot_sources?: string[];
}

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';
export type MessageType = 'text' | 'markdown' | 'json';

export interface RunMessage {
  id: string;
  invocation_id?: string | null;
  role: MessageRole;
  message_type: MessageType;
  content: string;
  created_at: string;
}

export interface ChatExecutionInfo {
  id: string;
  invocation_id: string;
  session_id: string;
  account_id: string;
  user_message_id?: string | null;
  status: string;
  error: string;
  interrupt_payload?: Record<string, unknown>;
}

export interface ChatSessionSummary {
  session_id: string;
  account_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  latest_status: string | null;
  message_count: number;
}

export interface ChatSessionDetail extends ChatSessionSummary {
  messages: RunMessage[];
  latest_execution: ChatExecutionInfo | null;
}

export type AccountDetail = Account;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init?.headers ?? {}) },
    ...init
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return (await response.json()) as T;
}

function authHeaders(): Record<string, string> {
  const token = accessTokenProvider()?.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type SseListener = (event: MessageEvent<string>) => void;

export class FetchEventStream {
  private readonly listeners = new Map<string, SseListener[]>();
  private readonly abortController = new AbortController();
  onerror: ((error?: unknown) => void | Promise<void>) | null = null;

  constructor(private readonly url: string, private readonly init: RequestInit = {}) {
    void this.start();
  }

  addEventListener(eventName: string, listener: SseListener) {
    const listeners = this.listeners.get(eventName) ?? [];
    listeners.push(listener);
    this.listeners.set(eventName, listeners);
  }

  close() {
    this.abortController.abort();
  }

  private async start() {
    try {
      const response = await fetch(this.url, {
        ...this.init,
        headers: { ...authHeaders(), ...(this.init.headers ?? {}) },
        signal: this.abortController.signal
      });
      if (!response.ok || !response.body) {
        throw new Error(response.statusText || `SSE request failed: ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split(/\r?\n\r?\n/);
        buffer = parts.pop() ?? '';
        for (const part of parts) {
          this.dispatch(part);
        }
      }
      if (buffer.trim()) {
        this.dispatch(buffer);
      }
    } catch (error) {
      if (!this.abortController.signal.aborted) {
        await this.onerror?.(error);
      }
    }
  }

  private dispatch(raw: string) {
    const lines = raw.split(/\r?\n/);
    let eventName = 'message';
    let eventId = '';
    const dataLines: string[] = [];
    for (const line of lines) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      if (line.startsWith('id:')) eventId = line.slice(3).trim();
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    const event = new MessageEvent<string>(eventName, {
      data: dataLines.join('\n'),
      lastEventId: eventId
    });
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener(event);
    }
  }
}

export class FetchPostEventStream extends FetchEventStream {
  constructor(url: string, payload: unknown) {
    super(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  }
}

export const api = {
  accounts: () => request<Account[]>('/api/accounts'),
  account: (accountId: string) => request<AccountDetail>(`/api/accounts/${accountId}`),
  createAccount: (payload: AccountPayload) =>
    request<AccountDetail>('/api/accounts', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  updateAccount: (accountId: string, payload: AccountUpdatePayload) =>
    request<AccountDetail>(`/api/accounts/${accountId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),
  deleteAccount: (accountId: string) =>
    fetch(`${API_BASE}/api/accounts/${accountId}`, {
      method: 'DELETE',
      headers: authHeaders()
    }).then(async (response) => {
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
    }),
  sessions: () => request<ChatSessionSummary[]>('/api/chat/sessions'),
  session: (sessionId: string) => request<ChatSessionDetail>(`/api/chat/sessions/${sessionId}`),
  createSession: (payload: { account_id: string }) =>
    request<{ session_id: string; account_id: string; title: string }>('/api/chat/sessions', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  deleteSession: (sessionId: string) =>
    fetch(`${API_BASE}/api/chat/sessions/${sessionId}`, {
      method: 'DELETE',
      headers: authHeaders()
    }).then(async (response) => {
      if (!response.ok) {
        const text = await response.text();
        const error = new Error(text || response.statusText) as Error & { status?: number };
        error.status = response.status;
        throw error;
      }
    }),
  sendMessageStream: (sessionId: string, payload: { message: string }) =>
    new FetchPostEventStream(`${API_BASE}/api/chat/sessions/${sessionId}/messages/stream`, payload),
  resumeSessionStream: (sessionId: string, payload: { message?: string; resume_value?: unknown }) =>
    new FetchPostEventStream(`${API_BASE}/api/chat/sessions/${sessionId}/resume/stream`, payload),
  cancelSession: (sessionId: string) =>
    request<ChatSessionDetail>(`/api/chat/sessions/${sessionId}/cancel`, { method: 'POST' })
};

