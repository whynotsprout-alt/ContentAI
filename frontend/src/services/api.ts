const configuredApiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
export const API_BASE = configuredApiBase.trim().replace(/\/$/, '');

export interface Account {
  id: string;
  name: string;
  description: string;
  style_prompt: string;
  audience: string;
  preferred_directions: string[];
  boundaries: string[];
  viral_patterns: string[];
}

export interface AccountPayload {
  id: string;
  name: string;
  description: string;
  audience: string;
  preferred_directions: string[];
  boundaries: string[];
  viral_patterns: string[];
  style_prompt: string;
}

export interface AccountUpdatePayload {
  name?: string;
  description?: string;
  audience?: string;
  preferred_directions?: string[];
  boundaries?: string[];
  viral_patterns?: string[];
  style_prompt?: string;
}

export interface HotspotPlatformOption {
  id: string;
  label: string;
  source?: string;
}

export interface SystemConfig {
  hotspot_platforms: string[];
  topic_filter_prompt: string;
  topic_scoring_prompt: string;
}

export type SystemConfigPayload = SystemConfig;

export type AccountDetail = Omit<Account, 'hotspot_platforms' | 'topic_filter_prompt' | 'topic_scoring_prompt'>;

export interface Artifact {
  id: string;
  kind: string;
  title: string;
  media_type: string;
  summary: string;
  url: string;
}

export interface RunInfo {
  id: string;
  session_id: string;
  account_id: string;
  user_message: string;
  status: string;
  next_stage: string;
  pending_payload: string;
  selected_topic_title: string | null;
  error: string;
  artifacts: Artifact[];
  steps: Array<{ name: string; label: string; status: string; error: string }>;
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
  createSession: () => request<{ session_id: string; title: string }>('/api/chat/sessions', { method: 'POST' }),
  createRun: (payload: { session_id?: string; account_id: string; message: string }) =>
    request<{ run_id: string; session_id: string; status: string }>('/api/runs', {
      method: 'POST',
      body: JSON.stringify(payload)
  }),
  hotspotPlatformOptions: () => request<HotspotPlatformOption[]>('/api/hotspot-platforms'),
  getSystemConfig: () => request<SystemConfig>('/api/system-config'),
  updateSystemConfig: (payload: SystemConfigPayload) =>
    request<SystemConfig>('/api/system-config', {
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  run: (runId: string) => request<RunInfo>(`/api/runs/${runId}`),
  artifactUrl: (artifact: Artifact) => `${API_BASE}${artifact.url}`,
  eventUrl: (runId: string) => `${API_BASE}/api/runs/${runId}/events`,
  selectTopic: (runId: string, topicIndex: number) =>
    request<RunInfo>(`/api/runs/${runId}/select-topic`, {
      method: 'POST',
      body: JSON.stringify({ topic_index: topicIndex })
    }),
  confirmHotspots: (runId: string) =>
    request<RunInfo>(`/api/runs/${runId}/confirm-hotspots`, {
      method: 'POST'
    }),
  confirmResearch: (runId: string) =>
    request<RunInfo>(`/api/runs/${runId}/confirm-research`, {
      method: 'POST'
    })
};
