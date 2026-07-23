import { readFile, readdir } from 'node:fs/promises';
import { extname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const groups = {
  api: [
    'agents', 'agent', 'createAgent', 'updateAgent', 'createAgentVersion', 'deleteAgent',
    'sessions', 'session', 'createSession', 'deleteSession', 'sendMessage',
    'executionEvents', 'runStatus', 'cancelRun', 'resumeRun'
  ],
  authApi: [
    'register', 'login', 'logout', 'me',
    'changePassword'
  ],
  adminApi: [
    'users', 'user', 'disable', 'enable', 'temporaryPassword', 'updateUser', 'sessions',
    'sessionDetail', 'sessionMessages', 'usage', 'modelConfig', 'probeModelConfig', 'updateModelConfig'
  ]
};

async function sourceCorpus(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const contents = await Promise.all(entries.flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return [sourceCorpus(path)];
    if (!['.ts', '.vue', '.mjs'].includes(extname(entry.name))) return [];
    if (path.endsWith(resolve('src/services/api.ts'))) return [];
    return [readFile(path, 'utf8')];
  }));
  return contents.join('\n');
}

describe('API client usage contract', () => {
  it('keeps every client method attached to a real caller', async () => {
    const corpus = [
      await sourceCorpus(resolve(process.cwd(), 'src')),
      await sourceCorpus(resolve(process.cwd(), 'tests')),
      await sourceCorpus(resolve(process.cwd(), 'scripts'))
    ].join('\n');

    for (const [client, methods] of Object.entries(groups)) {
      for (const method of methods) {
        expect(corpus, `${client}.${method} has no caller`).toContain(`${client}.${method}(`);
      }
    }
  });

  it('matches the paginated chat and structured interrupt contracts', async () => {
    const apiSource = await readFile(resolve(process.cwd(), 'src/services/api.ts'), 'utf8');

    expect(apiSource).toContain('export interface ChatSessionList');
    expect(apiSource).toContain('items: ChatSessionSummary[];');
    expect(apiSource).toContain('next_cursor: string | null;');
    expect(apiSource).toContain('sessions: (cursor = \'\', limit = 50)');
    expect(apiSource).toContain('session: (sessionId: string, cursor = \'\', limit = 50)');
    expect(apiSource).toContain('export interface PublicMemoryProposal');
    expect(apiSource).toContain('export interface PublicInterruptAction');
    expect(apiSource).toContain('export interface PublicInterrupt');
    expect(apiSource).toContain("export type ResumeDecision = 'approve' | 'reject';");
    expect(apiSource).toContain('interrupt: PublicInterrupt | null;');
    expect(apiSource).toContain('resumeRun: (runId: string, payload: { interrupt_id: string; decision: ResumeDecision })');
    expect(apiSource).not.toContain('interrupt_payload');
    expect(apiSource).not.toContain('agent_id: payload.agent_id');
  });

  it('removes self-service reset APIs and routes while retaining password changes', async () => {
    const [apiSource, routerSource, authViewSource, rootSource] = await Promise.all([
      readFile(resolve(process.cwd(), 'src/services/api.ts'), 'utf8'),
      readFile(resolve(process.cwd(), 'src/router.ts'), 'utf8'),
      readFile(resolve(process.cwd(), 'src/views/AuthView.vue'), 'utf8'),
      readFile(resolve(process.cwd(), 'src/Root.vue'), 'utf8')
    ]);

    expect(apiSource).not.toContain('forgotPassword');
    expect(apiSource).not.toContain('resetPassword');
    expect(apiSource).toContain('changePassword');
    expect(routerSource).not.toContain('forgot-password');
    expect(routerSource).not.toContain('reset-password');
    expect(authViewSource).not.toContain('forgot-password');
    expect(authViewSource).not.toContain('reset-password');
    expect(apiSource).toContain('must_change_password: boolean;');
    expect(apiSource).toContain('temporary_password_expires_at: string | null;');
    expect(routerSource).toContain("path: '/change-password'");
    expect(routerSource).toContain('auth.user?.must_change_password');
    expect(rootSource).toContain("window.addEventListener('contentai:auth-expired'");
  });
});
