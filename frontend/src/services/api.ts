const configuredApiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
export const API_BASE = configuredApiBase.trim().replace(/\/$/, '');

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
  role: MessageRole;
  message_type: MessageType;
  content: string;
  created_at: string;
}

export interface ChatSessionSummary {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  latest_run_id: string | null;
  latest_status: string | null;
  message_count: number;
}

export interface ChatSessionDetail extends ChatSessionSummary {
  messages: RunMessage[];
}

export type AccountDetail = Account;

export interface RunInfo {
  id: string;
  session_id: string;
  account_id: string;
  user_message: string;
  status: string;
  error: string;
  messages: RunMessage[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return (await response.json()) as T;
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
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  deleteAccount: (accountId: string) =>
    fetch(`${API_BASE}/api/accounts/${accountId}`, {
      method: 'DELETE'
    }).then(async (response) => {
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
    }),
  sessions: () => request<ChatSessionSummary[]>('/api/chat/sessions'),
  session: (sessionId: string) => request<ChatSessionDetail>(`/api/chat/sessions/${sessionId}`),
  createSession: () => request<{ session_id: string; title: string }>('/api/chat/sessions', { method: 'POST' }),
  createRun: (payload: { session_id?: string; account_id: string; message: string }) =>
    request<{ run_id: string; session_id: string; status: string; memory?: unknown }>('/api/chat/runs', {
      method: 'POST',
      body: JSON.stringify(payload)
  }),
  run: (runId: string) => request<RunInfo>(`/api/chat/runs/${runId}`),
  eventUrl: (runId: string) => `${API_BASE}/api/chat/runs/${runId}/events`
};

