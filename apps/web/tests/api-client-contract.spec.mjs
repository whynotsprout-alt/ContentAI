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
    'forgotPassword', 'resetPassword', 'changePassword'
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
});
