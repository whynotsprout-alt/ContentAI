const configuredApiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
export const API_BASE = configuredApiBase.trim().replace(/\/$/, '');

export interface AgentProfile {
  id: string;
  name: string;
  description: string;
  current_version: AgentVersion | null;
}

export interface AgentVersion {
  id: string;
  agent_id: string;
  version: number;
  topic_scoring_prompt: string;
  content_prompt: string;
  hotspot_sources: string[];
}

export interface AgentProfilePayload {
  name: string;
  description: string;
  topic_scoring_prompt: string;
  content_prompt: string;
  hotspot_sources: string[];
}

export interface AgentProfileUpdatePayload {
  name?: string;
  description?: string;
}

export interface AgentVersionPayload {
  topic_scoring_prompt: string;
  content_prompt: string;
  hotspot_sources: string[];
}

export type MessageRole = 'user' | 'assistant';
export type MessageType = 'text' | 'markdown';
export type ExecutionStatus =
  | 'pending'
  | 'running'
  | 'waiting_input'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface ApiErrorDetail {
  code: string;
  message: string;
  retryable: boolean;
  request_id?: string;
  session_id?: string;
  thread_id?: string;
  execution_id?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string;
  readonly detail: ApiErrorDetail;

  constructor(status: number, detail: Partial<ApiErrorDetail> & { message: string }) {
    super(detail.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = detail.code ?? '';
    this.retryable = detail.retryable ?? status >= 500;
    this.requestId = detail.request_id ?? '';
    this.detail = {
      code: this.code,
      message: detail.message,
      retryable: this.retryable,
      ...(detail.request_id ? { request_id: detail.request_id } : {}),
      ...(detail.session_id ? { session_id: detail.session_id } : {}),
      ...(detail.thread_id ? { thread_id: detail.thread_id } : {}),
      ...(detail.execution_id ? { execution_id: detail.execution_id } : {})
    };
  }
}

export interface RunMessage {
  id: string;
  invocation_id?: string | null;
  role: MessageRole;
  message_type: MessageType;
  content: string;
  created_at: string;
}

export interface SendMessageRequest {
  agent_id: string;
  message: string;
  message_id?: string;
  idempotency_key?: string;
}

export interface ChatExecutionInfo {
  id: string;
  session_id: string;
  status: ExecutionStatus;
  error?: { code: string; message: string; retryable: boolean } | null;
  interrupt_payload?: Record<string, unknown>;
  streaming_degraded?: boolean;
  streaming_degraded_reason?: string;
  queue_stage?: 'dispatching' | 'waiting_worker' | 'starting' | null;
}

export interface SubmittedMessage {
  session_id: string;
  message_id: string;
  execution_id: string;
  status: string;
  trace_id?: string | null;
}

export interface ChatSessionSummary {
  session_id: string;
  agent_id: string;
  agent_version_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  latest_execution_status: ExecutionStatus | null;
  message_count: number;
}

export interface ChatSessionDetail extends ChatSessionSummary {
  messages: RunMessage[];
  latest_execution: ChatExecutionInfo | null;
}

export type AgentProfileDetail = AgentProfile;

function normalizeApiErrorPayload(status: number, payload: unknown, fallback: string) {
  const envelope = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
  const rawDetail = envelope.detail ?? envelope.error ?? envelope;
  if (rawDetail && typeof rawDetail === 'object') {
    const detail = rawDetail as Record<string, unknown>;
    return new ApiError(status, {
      code: typeof detail.code === 'string' ? detail.code : '',
      message: typeof detail.message === 'string'
        ? detail.message
        : typeof detail.detail === 'string'
          ? detail.detail
          : fallback,
      retryable: typeof detail.retryable === 'boolean' ? detail.retryable : status >= 500,
      request_id: typeof detail.request_id === 'string' ? detail.request_id : undefined,
      session_id: typeof detail.session_id === 'string' ? detail.session_id : undefined,
      thread_id: typeof detail.thread_id === 'string' ? detail.thread_id : undefined,
      execution_id: typeof detail.execution_id === 'string' ? detail.execution_id : undefined
    });
  }
  const message = typeof rawDetail === 'string' ? rawDetail : fallback;
  return new ApiError(status, { message, code: '', retryable: status >= 500 });
}

async function apiErrorFromResponse(response: Response) {
  const raw = await response.text().catch(() => '');
  let payload: unknown = raw;
  if (raw) {
    try {
      payload = JSON.parse(raw) as unknown;
    } catch {
      // Plain-text errors are supported for auth and infrastructure responses.
    }
  }
  return normalizeApiErrorPayload(response.status, payload, response.statusText || `请求失败（${response.status}）`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init?.headers ?? {}) },
    credentials: 'include'
  });
  if (!response.ok) {
    const error = await apiErrorFromResponse(response);
    const isCredentialRequest = ['/api/auth/login', '/api/auth/register', '/api/auth/forgot-password', '/api/auth/reset-password'].includes(path);
    const accountUnavailable = error.status === 401 || (error.status === 403 && /disabled|禁用/i.test(`${error.code} ${error.message}`));
    if (!isCredentialRequest && accountUnavailable && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('contentai:auth-expired', { detail: error }));
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const csrf = document.cookie
    .split('; ')
    .find((item) => item.startsWith('contentai_csrf='))
    ?.split('=')
    .slice(1)
    .join('=');
  if (csrf) headers['X-CSRF-Token'] = decodeURIComponent(csrf);
  return headers;
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
        credentials: 'include',
        signal: this.abortController.signal
      });
      if (!response.ok || !response.body) {
        throw await apiErrorFromResponse(response);
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
      if (!this.abortController.signal.aborted) {
        this.dispatchEvent('close', {});
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

  private dispatchEvent(eventName: string, payload: unknown) {
    const event = new MessageEvent<string>(eventName, {
      data: JSON.stringify(payload),
      lastEventId: ''
    });
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener(event);
    }
  }
}

export class FetchPostEventStream extends FetchEventStream {
  constructor(url: string, payload: unknown, headers: Record<string, string> = {}) {
    super(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(payload)
    });
  }
}

export const api = {
  agents: () => request<AgentProfile[]>('/api/agents'),
  agent: (agentId: string) => request<AgentProfileDetail>(`/api/agents/${agentId}`),
  createAgent: (payload: AgentProfilePayload) =>
    request<AgentProfileDetail>('/api/agents', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  updateAgent: (agentId: string, payload: AgentProfileUpdatePayload) =>
    request<AgentProfileDetail>(`/api/agents/${agentId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),
  createAgentVersion: (agentId: string, payload: AgentVersionPayload) =>
    request<AgentVersion>(`/api/agents/${agentId}/versions`, {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  deleteAgent: (agentId: string) =>
    request<void>(`/api/agents/${agentId}`, { method: 'DELETE' }),
  sessions: () => request<ChatSessionSummary[]>('/api/chat/sessions'),
  session: (sessionId: string) => request<ChatSessionDetail>(`/api/chat/sessions/${sessionId}`),
  createSession: (payload: { agent_id: string }) =>
    request<{ session_id: string; agent_id: string; agent_version_id: string; title: string }>('/api/chat/sessions', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  deleteSession: (sessionId: string) =>
    request<void>(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' }),
  sendMessage: (sessionId: string, payload: SendMessageRequest) => {
    const headers: Record<string, string> = {};
    if (payload.idempotency_key) {
      headers['Idempotency-Key'] = payload.idempotency_key;
    }
    return request<SubmittedMessage>(`/api/chat/sessions/${sessionId}/messages`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        agent_id: payload.agent_id,
        message: payload.message,
        ...(payload.message_id ? { message_id: payload.message_id } : {}),
        ...(payload.idempotency_key ? { idempotency_key: payload.idempotency_key } : {})
      })
    });
  },
  executionEvents: (runId: string, afterSequence = 0) =>
    new FetchEventStream(`${API_BASE}/api/chat/runs/${runId}/events?after_sequence=${afterSequence}`),
  runStatus: (runId: string) => request<ChatExecutionInfo>(`/api/chat/runs/${runId}/status`),
  resumeRun: (runId: string, payload: { agent_id: string; message: string }) =>
    request<ChatExecutionInfo>(`/api/chat/runs/${runId}/resume`, {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  cancelRun: (runId: string) =>
    request<ChatExecutionInfo>(`/api/chat/runs/${runId}/cancel`, { method: 'POST' })
};

export interface CurrentUser {
  id: string;
  email: string;
  role: 'user' | 'admin';
  status: string;
  email_verified_at: string | null;
  password_changed_at: string;
  created_at: string;
  last_login_at: string | null;
}

export interface AdminUser extends CurrentUser {
  password_set: boolean;
  agent_count: number;
  conversation_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usage_call_count: number;
  missing_usage_call_count: number;
  usage_coverage: number;
}

export interface AdminUserList {
  items: AdminUser[];
  page: number;
  page_size: number;
  total: number;
}

export interface AdminSessionSummary {
  session_id: string;
  user_id: string;
  user_email: string;
  agent_id: string;
  title: string;
  message_count: number;
  latest_execution_status: string | null;
  updated_at: string;
}

export interface AdminSessionDetail extends AdminSessionSummary {
  messages: Array<Record<string, unknown>>;
}

export interface AdminUsageBucket {
  bucket: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  call_count: number;
  failed_call_count: number;
  average_latency_ms: number | null;
}

export const authApi = {
  register: (email: string, password: string) =>
    request<{ message: string }>('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password })
    }),
  login: (email: string, password: string) =>
    request<CurrentUser>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password })
    }),
  logout: () => request<void>('/api/auth/logout', { method: 'POST' }),
  me: () => request<CurrentUser>('/api/auth/me'),
  forgotPassword: (email: string) =>
    request<{ message: string }>('/api/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify({ email })
    }),
  resetPassword: (token: string, password: string) =>
    request<{ message: string }>('/api/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify({ token, password })
    }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ message: string }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
    })
};

export const adminApi = {
  users: (params: { search?: string; status?: string; page?: number; page_size?: number }) => {
    const query = new URLSearchParams();
    if (params.search) query.set('search', params.search);
    if (params.status) query.set('status', params.status);
    query.set('page', String(params.page ?? 1));
    query.set('page_size', String(params.page_size ?? 20));
    return request<AdminUserList>(`/api/admin/users?${query}`);
  },
  user: (userId: string) => request<AdminUser>(`/api/admin/users/${userId}`),
  disable: (userId: string) =>
    request<AdminUser>(`/api/admin/users/${userId}/disable`, { method: 'POST' }),
  enable: (userId: string) =>
    request<AdminUser>(`/api/admin/users/${userId}/enable`, { method: 'POST' }),
  passwordReset: (userId: string) =>
    request<{ message: string }>(`/api/admin/users/${userId}/password-reset`, {
      method: 'POST'
    }),
  updateUser: (userId: string, role: 'user' | 'admin') =>
    request<AdminUser>(`/api/admin/users/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify({ role })
    }),
  sessions: (userId: string, page = 1, pageSize = 30) =>
    request<{ items: AdminSessionSummary[]; page: number; page_size: number; total: number }>(
      `/api/admin/users/${userId}/sessions?page=${page}&page_size=${pageSize}`
    ),
  sessionDetail: (sessionId: string) => request<AdminSessionDetail>(`/api/admin/sessions/${sessionId}`),
  usage: (params: { group_by?: 'day' | 'model' | 'category'; user_id?: string } = {}) => {
    const query = new URLSearchParams();
    query.set('group_by', params.group_by ?? 'day');
    if (params.user_id) query.set('user_id', params.user_id);
    return request<{ items: AdminUsageBucket[] }>(`/api/admin/usage?${query}`);
  }
};

