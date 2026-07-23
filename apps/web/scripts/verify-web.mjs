import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, rmSync, statSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import axe from 'axe-core';
import { chromium } from 'playwright-core';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDir, '..');
const repoRoot = resolve(webRoot, '..', '..');
const outputDir = resolve(repoRoot, 'output', 'playwright');
const viteHost = process.env.VERIFY_WEB_HOST || '127.0.0.1';
const configuredVitePort = Number(process.env.VERIFY_WEB_PORT || 5180);
let vitePort = configuredVitePort;
assert.ok(Number.isInteger(vitePort) && vitePort > 0 && vitePort < 65536, `无效端口：${vitePort}`);
let baseUrl = `http://${viteHost}:${vitePort}`;
const viteEntry = resolve(webRoot, 'node_modules', 'vite', 'bin', 'vite.js');
const axeAudits = [];

mkdirSync(outputDir, { recursive: true });
rmSync(resolve(outputDir, 'verify-web-failure.png'), { force: true });

const fixedNow = '2026-07-15T09:52:00.000Z';
const authUser = {
  id: 'admin-1',
  email: 'admin@contentai.test',
  role: 'admin',
  status: 'active',
  email_verified_at: '2026-07-01T08:00:00.000Z',
  password_changed_at: '2026-07-01T08:05:00.000Z',
  created_at: '2026-07-01T08:00:00.000Z',
  last_login_at: fixedNow
};

const agentVersion = {
  id: 'version-finance-3',
  agent_id: 'agent-finance',
  version: 3,
  topic_scoring_prompt: '从受众相关性、事实强度、传播潜力、差异化角度和内容风险五个维度评分。',
  content_prompt: '先给结论，再解释原因；区分事实、判断和不确定信息。',
  hotspot_sources: ['36kr', 'cls', 'yicai', 'bilibili', 'xiaohongshu']
};

const agent = {
  id: 'agent-finance',
  name: '高百烈说财经',
  description: '把商业新闻、品牌动作和消费现象转化为清晰判断。',
  current_version: agentVersion
};

const workbenchSessionSummary = {
  session_id: 'session-market',
  agent_id: agent.id,
  title: '平台补贴与消费趋势',
  created_at: '2026-07-15T08:30:00.000Z',
  updated_at: fixedNow,
  latest_execution_status: 'completed',
  message_count: 4
};

const secondSessionSummary = {
  session_id: 'session-ai',
  agent_id: agent.id,
  title: 'AI 搜索产品观察',
  created_at: '2026-07-14T10:00:00.000Z',
  updated_at: '2026-07-14T11:20:00.000Z',
  latest_execution_status: 'completed',
  message_count: 2
};

const workbenchSession = {
  ...workbenchSessionSummary,
  messages: [
    {
      id: 'message-1',
      role: 'user',
      message_type: 'text',
      content: '最近的平台补贴战值得怎么写？',
      created_at: '2026-07-15T09:40:00.000Z'
    },
    {
      id: 'message-2',
      role: 'assistant',
      message_type: 'markdown',
      content: '先别急着写“价格战”。更值得关注的是：平台正在用补贴重新分配用户的决策入口。\n\n可以从消费频次、商家成本和平台留存三个角度展开。',
      created_at: '2026-07-15T09:41:00.000Z'
    },
    {
      id: 'message-3',
      role: 'user',
      message_type: 'text',
      content: '给我一个适合口播的开头。',
      created_at: '2026-07-15T09:48:00.000Z'
    },
    {
      id: 'message-4',
      role: 'assistant',
      message_type: 'markdown',
      content: '你以为平台又在撒钱，其实它们真正争夺的，是你下一次消费时第一个打开谁。',
      created_at: fixedNow
    }
  ],
  latest_execution: null
};

const targetUser = {
  id: 'user-creator',
  email: 'creator@contentai.test',
  role: 'user',
  status: 'active',
  email_verified_at: '2026-07-02T09:30:00.000Z',
  password_changed_at: '2026-07-02T09:35:00.000Z',
  created_at: '2026-07-02T09:30:00.000Z',
  last_login_at: '2026-07-15T09:20:00.000Z',
  password_set: true,
  agent_count: 2,
  conversation_count: 7,
  input_tokens: 47667,
  output_tokens: 4728,
  total_tokens: 52395,
  usage_call_count: 12,
  missing_usage_call_count: 1,
  usage_coverage: 0.92
};

const reviewerUser = {
  ...targetUser,
  id: 'user-reviewer',
  email: 'reviewer@contentai.test',
  role: 'admin',
  agent_count: 1,
  conversation_count: 3,
  input_tokens: 18200,
  output_tokens: 2100,
  total_tokens: 20300,
  usage_call_count: 6,
  missing_usage_call_count: 0,
  usage_coverage: 1
};

const adminSessionSummary = {
  session_id: 'admin-session-1',
  user_id: targetUser.id,
  user_email: targetUser.email,
  agent_id: agent.id,
  title: '财经热点选题讨论',
  status: 'active',
  message_count: 5,
  latest_execution_status: 'completed',
  updated_at: fixedNow
};

const olderAdminSession = {
  ...adminSessionSummary,
  session_id: 'admin-session-older',
  title: '更早的审计会话',
  message_count: 1,
  updated_at: '2026-07-13T08:00:00.000Z'
};

const adminSessionSummaries = [
  adminSessionSummary,
  ...Array.from({ length: 16 }, (_, index) => ({
    ...adminSessionSummary,
    session_id: `admin-session-${index + 2}`,
    title: `审计会话 ${String(index + 2).padStart(2, '0')}`,
    message_count: index + 2,
    updated_at: new Date(Date.parse(fixedNow) - (index + 1) * 3_600_000).toISOString()
  }))
];

const auditLongContent = [
  '这是一段用于验证窄屏换行的超长中文内容，连续书写时也不能撑开会话审计窗口。',
  'https://contentai.example.test/research/2026/07/15/a-very-long-path-without-natural-breakpoints?source=verification&campaign=responsive-administration',
  'const_extremely_long_identifier_without_spaces_or_breakpoints_for_responsive_audit_verification_0123456789'
].join('\n');

const adminSessionDetail = {
  ...adminSessionSummary,
  messages: [
    { id: 'audit-1', role: 'user', content: '你好' },
    { id: 'audit-2', role: 'assistant', content: '你好，我可以帮你筛选热点、梳理判断或起草内容。' },
    { id: 'audit-3', role: 'user', content: '获取热点' },
    { id: 'audit-4', role: 'assistant', content: '今天值得跟进的是平台补贴、消费品牌财报和 AI 搜索产品更新。' },
    { id: 'audit-5', role: 'user', content: '先分析平台补贴。' },
    { id: 'audit-long', role: 'assistant', content: auditLongContent }
  ]
};

const modelConfiguration = {
  configured: true,
  id: 'model-config-7',
  version: 7,
  provider: 'openai_compatible',
  base_url: 'https://gateway.example.test/v1',
  model_name: 'gpt-4.1-mini',
  api_key_hint: 'sk-…9X2Q',
  validated_at: fixedNow,
  created_at: fixedNow,
  created_by_user_id: authUser.id,
  created_by_email: authUser.email
};

const usageBuckets = [
  {
    bucket: '2026-07-15T00:00:00+00:00',
    input_tokens: 7900,
    output_tokens: 909,
    total_tokens: 8809,
    call_count: 3,
    failed_call_count: 0,
    average_latency_ms: 1280
  },
  {
    bucket: '2026-07-14T00:00:00+00:00',
    input_tokens: 39767,
    output_tokens: 3819,
    total_tokens: 43586,
    call_count: 9,
    failed_call_count: 0,
    average_latency_ms: 1450
  }
];

function chromeExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.PROGRAMFILES && resolve(process.env.PROGRAMFILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['PROGRAMFILES(X86)'] && resolve(process.env['PROGRAMFILES(X86)'], 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.LOCALAPPDATA && resolve(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'
  ].filter(Boolean);
  const executable = candidates.find((candidate) => existsSync(candidate));
  if (!executable) {
    throw new Error('未找到本机 Chrome。可通过 CHROME_PATH 指定 chrome.exe。');
  }
  return executable;
}

function startVite(port) {
  assert.ok(existsSync(viteEntry), `未找到 Vite 入口：${viteEntry}`);
  const output = [];
  const child = spawn(
    process.execPath,
    [viteEntry, '--host', viteHost, '--port', String(port), '--strictPort'],
    {
      cwd: webRoot,
      env: { ...process.env, BROWSER: 'none', VITE_API_BASE_URL: '' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    }
  );
  for (const stream of [child.stdout, child.stderr]) {
    stream?.on('data', (chunk) => {
      output.push(String(chunk));
      if (output.length > 80) output.shift();
    });
  }
  return { child, output };
}

async function waitForVite(server, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  let originMismatchCount = 0;
  while (Date.now() < deadline) {
    if (server.child.exitCode !== null) {
      throw new Error(`Vite 提前退出（${server.child.exitCode}）。\n${server.output.join('').trim()}`);
    }
    try {
      const response = await fetch(baseUrl, { signal: AbortSignal.timeout(1200) });
      if (response.ok) {
        const html = await response.text();
        if (html.includes('/src/main.ts')) return;
        originMismatchCount += 1;
        lastError = new Error(`${baseUrl} 返回了其他站点，而不是当前 Vite 源码入口`);
        if (originMismatchCount >= 3 && /ready in/i.test(server.output.join(''))) {
          const mismatch = new Error(lastError.message);
          mismatch.code = 'VITE_ORIGIN_MISMATCH';
          throw mismatch;
        }
      } else {
        lastError = new Error(`HTTP ${response.status}`);
      }
    } catch (error) {
      if (error?.code === 'VITE_ORIGIN_MISMATCH') throw error;
      lastError = error;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 200));
  }
  throw new Error(`等待 Vite 启动超时：${lastError instanceof Error ? lastError.message : String(lastError)}`);
}

async function startVerifiedVite() {
  const explicitPort = process.env.VERIFY_WEB_PORT !== undefined;
  const candidates = explicitPort
    ? [configuredVitePort]
    : [configuredVitePort, configuredVitePort + 1, configuredVitePort + 2, configuredVitePort + 3];
  let lastError = null;

  for (const candidate of candidates) {
    vitePort = candidate;
    baseUrl = `http://${viteHost}:${vitePort}`;
    const candidateServer = startVite(candidate);
    server = candidateServer;
    try {
      await waitForVite(candidateServer);
      if (candidate !== configuredVitePort) {
        console.warn(`[verify:web] ${configuredVitePort} 被外部服务占用或遮蔽，已使用 ${candidate} 完成独立验收。`);
      }
      return candidateServer;
    } catch (error) {
      lastError = error;
      await stopVite(candidateServer.child);
      if (explicitPort) break;
    }
  }

  throw lastError instanceof Error ? lastError : new Error('无法启动独立的 Vite 验收服务。');
}

function forceStopProcessTree(child) {
  if (!child || child.exitCode !== null || !child.pid) return;
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true
    });
    return;
  }
  child.kill('SIGTERM');
}

async function stopVite(child) {
  if (!child || child.exitCode !== null) return;
  forceStopProcessTree(child);
  if (child.exitCode !== null) return;
  await new Promise((resolveExit) => {
    const timer = setTimeout(() => {
      if (child.exitCode === null && process.platform !== 'win32') child.kill('SIGKILL');
      resolveExit();
    }, 3000);
    child.once('exit', () => {
      clearTimeout(timer);
      resolveExit();
    });
  });
}

async function fulfillJson(route, body, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    headers: { 'cache-control': 'no-store' },
    body: JSON.stringify(body)
  });
}

async function installApiMocks(page, state) {
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    state.apiCalls.push(`${method} ${path}${url.search}`);

    if (path === '/api/auth/me' && method === 'GET') {
      if (state.authenticated) return fulfillJson(route, authUser);
      return fulfillJson(route, { detail: { code: 'UNAUTHENTICATED', message: '请先登录', retryable: false } }, 401);
    }
    if (path === '/api/auth/login' && method === 'POST') {
      state.authenticated = true;
      return fulfillJson(route, authUser);
    }
    if (path === '/api/agents' && method === 'GET') return fulfillJson(route, state.agentDeleted ? [] : [agent]);
    if (path === `/api/agents/${agent.id}` && method === 'GET') {
      const response = state.agentDetailResponses?.shift();
      if (response?.delay) await new Promise((resolveDelay) => setTimeout(resolveDelay, response.delay));
      return fulfillJson(route, response ? { ...agent, name: response.name } : agent);
    }
    if (path === `/api/agents/${agent.id}` && method === 'DELETE') {
      state.agentDeleted = true;
      return fulfillJson(route, null);
    }
    if (path === '/api/chat/sessions' && method === 'GET') {
      return fulfillJson(route, {
        items: [workbenchSessionSummary, secondSessionSummary],
        next_cursor: null
      });
    }
    if (path === `/api/chat/sessions/${workbenchSessionSummary.session_id}` && method === 'GET') {
      return fulfillJson(route, workbenchSession);
    }
    if (path === `/api/chat/sessions/${secondSessionSummary.session_id}` && method === 'GET') {
      return fulfillJson(route, {
        ...secondSessionSummary,
        messages: [
          { id: 'ai-1', role: 'user', message_type: 'text', content: 'AI 搜索最近有什么变化？', created_at: secondSessionSummary.created_at },
          { id: 'ai-2', role: 'assistant', message_type: 'text', content: '产品正在从答案生成转向可验证的任务完成。', created_at: secondSessionSummary.updated_at }
        ],
        latest_execution: {
          id: 'run-interrupt',
          session_id: secondSessionSummary.session_id,
          status: 'waiting_input',
          error: null,
          interrupt: {
            interrupt_id: 'interrupt-verify-web',
            actions: [
              {
                tool_name: 'prepare_topic_research',
                purpose: '检索并整理 AI 搜索产品的近期变化',
                memory: null
              },
              {
                tool_name: 'remember',
                purpose: '保存本账号对可验证来源的偏好',
                memory: {
                  type: 'preference',
                  content: '优先呈现可追溯到原始链接的研究结论'
                }
              }
            ]
          }
        }
      });
    }
    if (path === '/api/admin/users' && method === 'GET') {
      return fulfillJson(route, { items: [targetUser, reviewerUser], next_cursor: null });
    }
    const adminUserSessionsMatch = path.match(/^\/api\/admin\/users\/([^/]+)\/sessions$/);
    if (adminUserSessionsMatch && method === 'GET') {
      const user = [targetUser, reviewerUser].find((item) => item.id === adminUserSessionsMatch[1]);
      if (!user) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'user not found' } }, 404);
      return url.searchParams.has('cursor')
        ? fulfillJson(route, { items: [olderAdminSession], next_cursor: null })
        : fulfillJson(route, { items: adminSessionSummaries, next_cursor: 'session-cursor-1' });
    }
    if (path === '/api/admin/usage' && method === 'GET') {
      return fulfillJson(route, { items: usageBuckets });
    }
    const adminStatusChangeMatch = path.match(/^\/api\/admin\/users\/([^/]+)\/(disable|enable)$/);
    if (adminStatusChangeMatch && method === 'POST') {
      const [, userId, action] = adminStatusChangeMatch;
      const user = [targetUser, reviewerUser].find((item) => item.id === userId);
      if (!user) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'user not found' } }, 404);
      if (state.adminStatusFailureUserId === userId) {
        return fulfillJson(route, { detail: { code: 'USER_STATUS_UNAVAILABLE', message: '用户状态服务暂不可用' } }, 503);
      }
      return fulfillJson(route, { ...user, status: action === 'disable' ? 'disabled' : 'active' });
    }
    const temporaryPasswordMatch = path.match(/^\/api\/admin\/users\/([^/]+)\/temporary-password$/);
    if (temporaryPasswordMatch && method === 'POST') {
      const userId = temporaryPasswordMatch[1];
      const user = [targetUser, reviewerUser].find((item) => item.id === userId);
      if (!user) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'user not found' } }, 404);
      const delay = state.temporaryPasswordDelays?.[userId] ?? 0;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      if (state.temporaryPasswordFailureUserId === userId) {
        return fulfillJson(route, { detail: { code: 'PASSWORD_SERVICE_UNAVAILABLE', message: '临时密码服务暂不可用' } }, 503);
      }
      state.temporaryPasswordCount = (state.temporaryPasswordCount ?? 0) + 1;
      return fulfillJson(route, {
        temporary_password: state.temporaryPasswordSecrets?.[userId] ?? `one-time-secret-${state.temporaryPasswordCount}`,
        expires_at: '2026-07-15T10:07:00.000Z'
      });
    }
    const adminUserMatch = path.match(/^\/api\/admin\/users\/([^/]+)$/);
    if (adminUserMatch && method === 'PATCH') {
      const userId = adminUserMatch[1];
      const user = [targetUser, reviewerUser].find((item) => item.id === userId);
      if (!user) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'user not found' } }, 404);
      if (state.adminRoleFailureUserId === userId) {
        return fulfillJson(route, { detail: { code: 'USER_ROLE_UNAVAILABLE', message: '用户角色服务暂不可用' } }, 503);
      }
      const payload = request.postDataJSON();
      return fulfillJson(route, { ...user, role: payload.role });
    }
    if (adminUserMatch && method === 'GET') {
      const userId = adminUserMatch[1];
      const user = [targetUser, reviewerUser].find((item) => item.id === userId);
      if (!user) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'user not found' } }, 404);
      const delay = state.adminUserDetailDelays?.[userId] ?? 0;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      if (state.adminUserDetailFailureUserId === userId) {
        return fulfillJson(route, { detail: { code: 'USER_DETAIL_UNAVAILABLE', message: '用户详情暂不可用' } }, 502);
      }
      return fulfillJson(route, user);
    }
    const adminMessageMatch = path.match(/^\/api\/admin\/sessions\/([^/]+)\/messages$/);
    if (adminMessageMatch && method === 'GET') {
      const session = adminSessionSummaries.find((item) => item.session_id === adminMessageMatch[1]);
      if (!session) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'session not found' } }, 404);
      const delay = state.adminSessionDelays?.[session.session_id] ?? 0;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      if (state.adminSessionFailureId === session.session_id) {
        return fulfillJson(route, { detail: { code: 'SESSION_MESSAGES_UNAVAILABLE', message: '会话消息暂不可用' } }, 502);
      }
      return url.searchParams.has('cursor')
        ? fulfillJson(route, { items: [{ id: 'audit-6', role: 'assistant', message_type: 'text', content: '补充加载的审计消息。', created_at: fixedNow }], next_cursor: null })
        : fulfillJson(route, { items: adminSessionDetail.messages, next_cursor: 'message-cursor-1' });
    }
    const adminDetailMatch = path.match(/^\/api\/admin\/sessions\/([^/]+)$/);
    if (adminDetailMatch && method === 'GET') {
      const session = adminSessionSummaries.find((item) => item.session_id === adminDetailMatch[1]);
      if (!session) return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'session not found' } }, 404);
      const delay = state.adminSessionDelays?.[session.session_id] ?? 0;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      if (state.adminSessionFailureId === session.session_id) {
        return fulfillJson(route, { detail: { code: 'SESSION_DETAIL_UNAVAILABLE', message: '会话详情暂不可用' } }, 502);
      }
      return fulfillJson(route, { ...adminSessionDetail, ...session });
    }
    if (path === '/api/admin/model-config' && method === 'GET') {
      if (state.modelLoadFailure) {
        return fulfillJson(route, { detail: { code: 'MODEL_PROVIDER_UNREACHABLE', message: 'safe load failure' } }, 502);
      }
      return fulfillJson(route, { ...modelConfiguration, version: state.modelVersion });
    }
    if (path === '/api/admin/model-config' && method === 'PUT') {
      state.modelPutPayloads.push(request.postDataJSON());
      state.modelVersion = 8;
      return fulfillJson(route, { detail: { code: 'MODEL_CONFIG_CHANGED', message: 'configuration changed' } }, 409);
    }
    if (path === '/api/admin/model-config/probe' && method === 'POST') {
      return fulfillJson(route, {
        base_url: modelConfiguration.base_url,
        models: ['gpt-4.1', 'gpt-4.1-mini', 'o4-mini'],
        models_truncated: false,
        model_validated: false,
        latency_ms: 248
      });
    }

    state.unexpectedApiCalls.push(`${method} ${path}${url.search}`);
    return fulfillJson(
      route,
      { detail: { code: 'VERIFY_WEB_UNMOCKED', message: `验收脚本未模拟 ${method} ${path}`, retryable: false } },
      501
    );
  });
}

async function expectVisible(locator, label) {
  await locator.waitFor({ state: 'visible' });
  assert.equal(await locator.isVisible(), true, `${label} 应可见`);
}

async function expectHidden(locator, label) {
  await locator.waitFor({ state: 'hidden' });
  assert.equal(await locator.isHidden(), true, `${label} 应隐藏`);
}

async function expectFocused(locator, label) {
  await locator.waitFor({ state: 'visible' });
  await locator.evaluate((element) => new Promise((resolvePromise) => {
    const deadline = performance.now() + 1200;
    const check = () => {
      if (document.activeElement === element || performance.now() >= deadline) {
        resolvePromise();
        return;
      }
      requestAnimationFrame(check);
    };
    check();
  }));
  assert.equal(await locator.evaluate((element) => element === document.activeElement), true, label);
}

async function expectElementWidth(locator, expected, label) {
  await locator.waitFor({ state: 'attached' });
  await locator.evaluate((element, target) => new Promise((resolvePromise) => {
    const deadline = performance.now() + 1200;
    const check = () => {
      if (Math.round(element.getBoundingClientRect().width) === target || performance.now() >= deadline) {
        resolvePromise();
        return;
      }
      requestAnimationFrame(check);
    };
    check();
  }), expected);
  assert.equal(
    Math.round(await locator.evaluate((element) => element.getBoundingClientRect().width)),
    expected,
    label
  );
}

async function expectInsideViewport(locator, page, label) {
  await expectVisible(locator, label);
  const [box, viewport] = await Promise.all([
    locator.boundingBox(),
    page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight }))
  ]);
  assert.ok(box, `${label} 应具有可测量边界`);
  assert.ok(box.x >= 0, `${label} 左侧越出视口：${JSON.stringify(box)}`);
  assert.ok(box.y >= 0, `${label} 顶部越出视口：${JSON.stringify(box)}`);
  assert.ok(box.x + box.width <= viewport.width + 0.5, `${label} 右侧越出视口：${JSON.stringify({ box, viewport })}`);
  assert.ok(box.y + box.height <= viewport.height + 0.5, `${label} 底部越出视口：${JSON.stringify({ box, viewport })}`);
}

async function expectNoHorizontalOverflow(locator, label) {
  await locator.waitFor({ state: 'visible' });
  const dimensions = await locator.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth
  }));
  assert.ok(
    dimensions.scrollWidth <= dimensions.clientWidth + 1,
    `${label} 不应水平溢出：${JSON.stringify(dimensions)}`
  );
}

async function expectAxeClean(page, stateName) {
  if (!await page.evaluate(() => Boolean(window.axe))) {
    await page.addScriptTag({ content: axe.source });
  }
  const result = await page.evaluate(async () => window.axe.run(document, {
    resultTypes: ['violations']
  }));
  const viewport = await page.evaluate(() => `${window.innerWidth}x${window.innerHeight}`);
  const route = new URL(page.url()).pathname;
  const blocking = result.violations
    .filter((violation) => ['serious', 'critical'].includes(violation.impact))
    .flatMap((violation) => violation.nodes.map((node) => ({
      state: stateName,
      route,
      viewport,
      ruleId: violation.id,
      impact: violation.impact,
      target: node.target,
      summary: node.failureSummary
    })));
  axeAudits.push({
    state: stateName,
    route,
    viewport,
    violationCount: result.violations.length,
    seriousCriticalCount: blocking.length
  });
  assert.deepEqual(
    blocking,
    [],
    `[axe] state=${stateName} route=${route} viewport=${viewport} serious/critical violations:\n${blocking
      .map((item) => `${item.ruleId} impact=${item.impact} target=${item.target.join(' ')} — ${item.summary}`)
      .join('\n')}`
  );
}

async function expectMinimumTouchTargets(locator, label) {
  await locator.waitFor({ state: 'visible' });
  const failures = await locator.evaluate((root) => {
    const selectorFor = (element) => {
      if (element.id) return `#${CSS.escape(element.id)}`;
      const testId = element.getAttribute('data-testid');
      if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
      const className = Array.from(element.classList).slice(0, 2).map((name) => `.${CSS.escape(name)}`).join('');
      return `${element.tagName.toLowerCase()}${className}`;
    };
    return Array.from(root.querySelectorAll('button, a[href], input, textarea, select, summary'))
      .filter((element) => {
        if (!(element instanceof HTMLElement) || element.getClientRects().length === 0) return false;
        if ('disabled' in element && element.disabled) return false;
        return getComputedStyle(element).visibility !== 'hidden';
      })
      .map((element) => {
        const { width, height } = element.getBoundingClientRect();
        return {
          selector: selectorFor(element),
          width: Math.round(width * 10) / 10,
          height: Math.round(height * 10) / 10
        };
      })
      .filter(({ width, height }) => width < 44 || height < 44);
  });
  assert.deepEqual(
    failures,
    [],
    `${label} 可见触控目标必须至少 44x44：${failures
      .map(({ selector, width, height }) => `${selector}=${width}x${height}`)
      .join(', ')}`
  );
}

async function expectPlainAdminSurfaces(page, label) {
  const decorated = await page.locator([
    '.admin-topbar',
    '.admin-workspace',
    '.admin-list-panel',
    '.admin-detail-panel',
    '.model-config-panel',
    '.model-status-panel',
    '.model-status-summary',
    '.model-status-details',
    '.admin-audit-window',
    '.admin-audit-header',
    '.admin-audit-sessions',
    '.admin-transcript',
    '.admin-session-transcript',
    '.admin-session-transcript article p'
  ].join(',')).evaluateAll((elements) => elements
    .filter((element) => element.getClientRects().length > 0)
    .map((element) => {
      const style = getComputedStyle(element);
      return {
        className: element.className,
        backgroundImage: style.backgroundImage,
        boxShadow: style.boxShadow
      };
    })
    .filter((style) => style.backgroundImage !== 'none' || style.boxShadow !== 'none'));
  assert.deepEqual(decorated, [], `${label} 主页面 surface 不应使用投影或装饰背景：${JSON.stringify(decorated)}`);
}

async function expectSingleVerticalScrollContainer(locator, label) {
  await locator.waitFor({ state: 'visible' });
  const containers = await locator.evaluate((root) => Array.from(root.querySelectorAll('*'))
    .filter((element) => {
      if (!(element instanceof HTMLElement) || element.getClientRects().length === 0) return false;
      const overflowY = getComputedStyle(element).overflowY;
      return ['auto', 'scroll'].includes(overflowY) && element.scrollHeight > element.clientHeight + 1;
    })
    .map((element) => ({
      className: element.className,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight
    })));
  assert.equal(
    containers.length,
    1,
    `${label} 应只有一个 vertical scroll container：${JSON.stringify(containers)}`
  );
}

async function expectSolidProductDialog(locator, label) {
  await locator.waitFor({ state: 'visible' });
  const style = await locator.evaluate((element) => {
    const computed = getComputedStyle(element);
    return {
      backdropFilter: computed.backdropFilter,
      webkitBackdropFilter: computed.webkitBackdropFilter,
      backgroundColor: computed.backgroundColor,
      backgroundImage: computed.backgroundImage
    };
  });
  assert.equal(style.backdropFilter, 'none', `${label} 不应使用 backdrop-filter`);
  assert.ok(
    !style.webkitBackdropFilter || style.webkitBackdropFilter === 'none',
    `${label} 不应使用 -webkit-backdrop-filter`
  );
  assert.equal(style.backgroundImage, 'none', `${label} 不应使用渐变/背景图`);
  assert.equal(style.backgroundColor, 'rgb(255, 255, 255)', `${label} 应使用实色产品 surface`);
}

async function expectNoHoverLift(locator, page, label) {
  await locator.hover();
  await page.waitForTimeout(240);
  const transform = await locator.evaluate((element) => getComputedStyle(element).transform);
  const matrix = transform === 'none'
    ? { a: 1, d: 1, e: 0, f: 0 }
    : await page.evaluate((value) => {
        const parsed = new DOMMatrixReadOnly(value);
        return { a: parsed.a, d: parsed.d, e: parsed.e, f: parsed.f };
      }, transform);
  assert.deepEqual(
    matrix,
    { a: 1, d: 1, e: 0, f: 0 },
    `${label} hover 不应产生位移、缩放或抬升：${transform}`
  );
}

async function expectProductDrawerRadii(page) {
  const radii = await page.evaluate(() => {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop modal-drawer';
    backdrop.dataset.surface = 'product';
    const drawer = document.createElement('section');
    drawer.className = 'accessible-dialog accessible-dialog-drawer';
    backdrop.append(drawer);
    document.body.append(backdrop);
    const style = getComputedStyle(drawer);
    const result = {
      topLeft: style.borderTopLeftRadius,
      topRight: style.borderTopRightRadius,
      bottomRight: style.borderBottomRightRadius,
      bottomLeft: style.borderBottomLeftRadius
    };
    backdrop.remove();
    return result;
  });
  assert.deepEqual(radii, {
    topLeft: '16px',
    topRight: '0px',
    bottomRight: '0px',
    bottomLeft: '16px'
  }, '产品 drawer 应贴右且只保留左侧圆角');
}

async function settleVisuals(page) {
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
  });
  await page.waitForTimeout(120);
}

async function capture(page, fileName, screenshots) {
  await settleVisuals(page);
  const path = resolve(outputDir, fileName);
  await page.screenshot({ path, animations: 'disabled' });
  assert.ok(statSync(path).size > 1000, `截图为空：${path}`);
  screenshots.push(path);
}

async function runDesktopAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    colorScheme: 'light',
    reducedMotion: 'reduce',
    deviceScaleFactor: 1,
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  page.setDefaultNavigationTimeout(20000);

  const state = {
    authenticated: false,
    apiCalls: [],
    unexpectedApiCalls: [],
    pageErrors: [],
    consoleErrors: [],
    failedResponses: [],
    failedRequests: [],
    mediaRequests: [],
    modelVersion: 7,
    modelLoadFailure: false,
    modelPutPayloads: [],
    agentDeleted: false,
    agentDetailResponses: []
  };
  const screenshots = [];
  let authRegisterLayout = null;
  page.on('pageerror', (error) => state.pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') state.consoleErrors.push(message.text());
  });
  page.on('response', (response) => {
    const expectedAnonymousMe = !state.authenticated && response.url().includes('/api/auth/me') && response.status() === 401;
    const expectedModelConflict = response.request().method() === 'PUT' && response.url().includes('/api/admin/model-config') && response.status() === 409;
    const expectedModelLoadFailure = state.modelLoadFailure && response.request().method() === 'GET' && response.url().includes('/api/admin/model-config') && response.status() === 502;
    if (response.status() >= 400 && !expectedAnonymousMe && !expectedModelConflict && !expectedModelLoadFailure) {
      state.failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on('requestfailed', (request) => {
    state.failedRequests.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText ?? 'unknown'}`);
  });
  page.on('request', (request) => {
    if (/\.mp4(?:$|\?)/i.test(request.url())) state.mediaRequests.push(request.url());
  });
  await installApiMocks(page, state);

  try {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '欢迎回到 ContentAI', exact: true }), '登录标题');
    await expectVisible(page.getByRole('textbox', { name: '邮箱', exact: true }), '邮箱输入框');
    await expectVisible(page.getByRole('button', { name: '登录', exact: true }), '登录按钮');
    await expectHidden(page.locator('.desktop-gate'), '桌面门槛');
    assert.equal(await page.locator('.ambient-backdrop__video').count(), 0, '浅色静态背景不应渲染视频节点');
    assert.equal(await page.locator('.ambient-backdrop').getAttribute('data-material'), 'web-background');
    assert.equal(await page.locator('.ambient-backdrop').getAttribute('data-video-state'), 'poster');
    await expectAxeClean(page, 'auth-login');
    await expectMinimumTouchTargets(page.locator('.auth-form'), '1440x900 登录');
    await capture(page, '01-auth-login-1440x900.png', screenshots);

    await page.setViewportSize({ width: 1280, height: 800 });
    await expectVisible(page.getByRole('heading', { name: '欢迎回到 ContentAI', exact: true }), '1280px 登录标题');
    await expectNoHorizontalOverflow(page.locator('html'), '1280x800 登录整页');
    await capture(page, '01a-auth-login-1280x800.png', screenshots);

    await page.getByRole('link', { name: '创建账号', exact: true }).click();
    await page.waitForURL('**/register');
    await expectVisible(page.getByRole('heading', { name: '创建你的内容空间', exact: true }), '注册标题');
    await expectAxeClean(page, 'auth-register');
    await expectMinimumTouchTargets(page.locator('.auth-form'), '1280x800 注册');
    await capture(page, '01d-auth-register-1280x800.png', screenshots);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForFunction(() => window.scrollY === 0);
    await settleVisuals(page);
    authRegisterLayout = await page.evaluate(() => {
      const hero = document.querySelector('.auth-hero')?.getBoundingClientRect();
      const eyebrow = document.querySelector('.auth-hero__copy .section-kicker')?.getBoundingClientRect();
      const title = document.querySelector('#auth-hero-title')?.getBoundingClientRect();
      return {
        scrollY: window.scrollY,
        viewportHeight: window.innerHeight,
        pageHeight: document.documentElement.scrollHeight,
        hero: hero && { top: hero.top, bottom: hero.bottom, height: hero.height },
        eyebrow: eyebrow && { top: eyebrow.top, bottom: eyebrow.bottom, height: eyebrow.height },
        title: title && { top: title.top, bottom: title.bottom, height: title.height }
      };
    });
    assert.equal(authRegisterLayout.scrollY, 0, '1440x900 注册页截图前滚动位置应归零');
    assert.ok(authRegisterLayout.hero, '1440x900 注册页 hero 应可测量');
    assert.ok(authRegisterLayout.eyebrow, '1440x900 注册页 eyebrow 应可测量');
    assert.ok(authRegisterLayout.title, '1440x900 注册页标题应可测量');
    assert.ok(authRegisterLayout.hero.top >= 0, `注册 hero 顶部被裁切：${JSON.stringify(authRegisterLayout)}`);
    assert.ok(
      authRegisterLayout.hero.bottom <= authRegisterLayout.viewportHeight,
      `注册 hero 底部被裁切：${JSON.stringify(authRegisterLayout)}`
    );
    assert.ok(
      authRegisterLayout.eyebrow.top >= authRegisterLayout.hero.top + 16,
      `注册 eyebrow 贴近或越过 hero 顶部：${JSON.stringify(authRegisterLayout)}`
    );
    assert.ok(
      authRegisterLayout.title.top >= authRegisterLayout.eyebrow.bottom,
      `注册标题与 eyebrow 发生裁切或重叠：${JSON.stringify(authRegisterLayout)}`
    );
    await capture(page, '01e-auth-register-1440x900.png', screenshots);

    await page.getByRole('link', { name: '返回登录', exact: true }).click();
    await page.waitForURL('**/login');
    await page.setViewportSize({ width: 768, height: 1024 });
    await expectVisible(page.getByRole('heading', { name: '欢迎回到 ContentAI', exact: true }), '768px 登录标题');
    await capture(page, '01b-auth-login-768x1024.png', screenshots);
    await page.setViewportSize({ width: 360, height: 800 });
    await expectVisible(page.getByRole('button', { name: '登录', exact: true }), '360px 登录按钮');
    await capture(page, '01c-auth-login-360x800.png', screenshots);
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.getByRole('textbox', { name: '邮箱', exact: true }).fill(authUser.email);
    await page.locator('input[type="password"]').first().fill('verify-web-password');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.waitForURL('**/app');

    await expectVisible(page.locator('.workbench-shell'), '工作台');
    await expectVisible(page.getByText(agent.name, { exact: true }).first(), '当前内容账号');
    await expectVisible(page.getByText('平台补贴与消费趋势', { exact: true }), '会话条目');
    await expectVisible(page.getByText('你以为平台又在撒钱，其实它们真正争夺的，是你下一次消费时第一个打开谁。', { exact: true }), '对话消息');
    await expectAxeClean(page, 'workbench-messages');
    const agentPicker = page.locator('.agent-picker-button');
    await agentPicker.click();
    const firstAgentOption = page.locator('#agent-picker-options button').first();
    await expectVisible(firstAgentOption, '原生内容账号选项');
    await page.keyboard.press('Tab');
    await expectFocused(firstAgentOption, 'Tab 应进入第一个原生内容账号按钮');
    await page.keyboard.press('Escape');
    await expectHidden(page.locator('#agent-picker-options'), 'Escape 应关闭内容账号 disclosure');
    await expectFocused(agentPicker, 'Escape 应把焦点归还内容账号触发器');
    await capture(page, '02-workbench-1440x900.png', screenshots);

    const sessionRail = page.locator('#session-navigation');
    await expectElementWidth(sessionRail, 280, '1440px 侧栏应为 280px');

    await page.setViewportSize({ width: 1920, height: 1080 });
    await expectElementWidth(sessionRail, 280, '1920px 侧栏应为 280px');
    await expectNoHorizontalOverflow(page.locator('html'), '1920x1080 工作台整页');
    await capture(page, '02a-workbench-1920x1080.png', screenshots);

    await page.setViewportSize({ width: 1280, height: 720 });
    await expectElementWidth(sessionRail, 264, '1280x720 侧栏应为 264px');
    await expectNoHorizontalOverflow(page.locator('html'), '1280x720 工作台整页');
    await capture(page, '02aa-workbench-1280x720.png', screenshots);

    await page.setViewportSize({ width: 1280, height: 800 });
    await expectElementWidth(sessionRail, 264, '1280px 侧栏应为 264px');
    await capture(page, '02b-workbench-1280x800.png', screenshots);

    await page.setViewportSize({ width: 1024, height: 768 });
    await expectElementWidth(sessionRail, 248, '1024px 侧栏应为 248px');
    await expectVisible(page.getByText('平台补贴与消费趋势', { exact: true }), '1024px 会话条目');
    await capture(page, '02c-workbench-1024x768.png', screenshots);

    await page.setViewportSize({ width: 768, height: 1024 });
    const sessionToggle = page.getByRole('button', { name: '打开会话导航', exact: true });
    await expectVisible(sessionToggle, '平板会话导航开关');
    await expectElementWidth(sessionRail, 72, '768px 默认侧栏应为 72px');
    assert.equal(await sessionRail.getAttribute('role'), 'navigation', '非 drawer 会话栏应使用 navigation 语义');
    assert.equal(await sessionRail.getAttribute('aria-modal'), null, '非 drawer 会话栏不应声明 aria-modal');
    await capture(page, '02d-workbench-768x1024-collapsed.png', screenshots);
    await sessionToggle.click();
    await expectElementWidth(sessionRail, 280, '768px 展开侧栏应为 280px');
    const expandedTabletElements = [
      ['768px 品牌入口', page.getByRole('link', { name: 'ContentAI，跳到对话区', exact: true })],
      ['768px 内容账号入口', page.getByRole('button', { name: '内容账号', exact: true })],
      ['768px 管理后台入口', page.getByRole('button', { name: '管理后台', exact: true })],
      ['768px 内容账号选择器', page.locator('.agent-picker-button')],
      ['768px 账号菜单', page.locator('.user-menu-button')],
      ['768px composer', page.locator('.composer')],
      ['768px composer 提示', page.locator('.composer-hint-desktop')]
    ];
    for (const [label, locator] of expandedTabletElements) {
      await expectInsideViewport(locator, page, label);
    }
    await expectNoHorizontalOverflow(page.locator('.conversation-column'), '768px 展开态对话列');
    await expectNoHorizontalOverflow(page.locator('.composer-wrap'), '768px 展开态 composer 区');
    await capture(page, '02e-workbench-768x1024-expanded.png', screenshots);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expectVisible(page.locator('.workbench-shell'), '平板刷新后的工作台');
    await expectElementWidth(sessionRail, 280, '平板展开状态应跨刷新持久化');

    await page.setViewportSize({ width: 360, height: 800 });
    await expectHidden(sessionRail, '手机默认隐藏的会话抽屉');
    const mobileToggle = page.getByRole('button', { name: '打开会话导航', exact: true });
    await mobileToggle.click();
    await expectVisible(sessionRail, '手机会话抽屉');
    await expectElementWidth(sessionRail, 360, '360px 手机抽屉应全屏');
    assert.equal(await sessionRail.evaluate((element) => element.contains(document.activeElement)), true, '打开抽屉后焦点应进入抽屉');
    await capture(page, '02f-workbench-360x800-drawer.png', screenshots);
    await page.keyboard.press('Escape');
    await expectHidden(sessionRail, 'Escape 关闭手机会话抽屉');
    assert.equal(await mobileToggle.evaluate((element) => element === document.activeElement), true, '关闭抽屉后焦点应归还开关');
    await mobileToggle.click();
    await page.locator('.session-select').filter({ hasText: '平台补贴与消费趋势' }).click();
    await expectHidden(sessionRail, '选择会话后关闭手机抽屉');
    const mobilePrompt = page.getByRole('textbox', { name: '对话输入', exact: true });
    await mobilePrompt.fill('移动端 Enter 应换行');
    await mobilePrompt.press('Enter');
    assert.equal((await mobilePrompt.inputValue()).endsWith('\n'), true, '手机 Enter 应插入换行');
    await mobilePrompt.fill('');
    await capture(page, '02g-workbench-360x800.png', screenshots);
    await page.locator('.user-menu-button').click();
    await expectVisible(page.locator('#user-account-menu').getByRole('button', { name: '内容账号', exact: true }), '手机内容账号入口');
    await expectVisible(page.locator('#user-account-menu').getByRole('button', { name: '管理后台', exact: true }), '手机管理后台入口');
    await expectVisible(page.locator('#user-account-menu').getByRole('button', { name: '修改密码', exact: true }), '手机改密入口');
    await expectVisible(page.locator('#user-account-menu').getByRole('button', { name: '退出登录', exact: true }), '手机退出入口');
    await page.keyboard.press('Escape');

    await page.setViewportSize({ width: 390, height: 844 });
    const mediumPhoneToggle = page.getByRole('button', { name: '打开会话导航', exact: true });
    await mediumPhoneToggle.click();
    await expectVisible(sessionRail, '390px 手机会话抽屉');
    await expectElementWidth(sessionRail, 360, '390px 手机抽屉应严格限制为 360px');
    assert.equal(await sessionRail.getAttribute('role'), 'dialog', '手机会话抽屉应使用 dialog 语义');
    assert.equal(await sessionRail.getAttribute('aria-modal'), 'true', '手机会话抽屉应声明 aria-modal');
    await expectInsideViewport(sessionRail, page, '390px 手机会话抽屉');
    await expectAxeClean(page, 'session-drawer');
    await expectMinimumTouchTargets(sessionRail, '390x844 会话抽屉');
    await capture(page, '02h-workbench-390x844-drawer.png', screenshots);
    await page.keyboard.press('Escape');

    await page.setViewportSize({ width: 320, height: 800 });
    const narrowPhoneToggle = page.getByRole('button', { name: '打开会话导航', exact: true });
    await narrowPhoneToggle.click();
    await expectElementWidth(sessionRail, 320, '320px 手机抽屉应全屏');
    await expectNoHorizontalOverflow(page.locator('html'), '320x800 工作台整页');
    await expectMinimumTouchTargets(page.locator('.workbench-shell'), '320x800 工作台');
    await capture(page, '02ha-workbench-320x800-drawer.png', screenshots);
    await page.keyboard.press('Escape');

    await page.setViewportSize({ width: 600, height: 800 });
    await page.getByRole('button', { name: '打开会话导航', exact: true }).click();
    await page.locator('.session-rail-scrim').click({ position: { x: 540, y: 400 } });
    await expectHidden(sessionRail, '遮罩关闭手机会话抽屉');

    await page.setViewportSize({ width: 1440, height: 900 });
    assert.equal(state.mediaRequests.length, 0, 'reduced-motion 登录及工作台不应请求 MP4');

    await page.locator('.user-menu-button').click();
    await page.locator('#user-account-menu').getByRole('button', { name: '修改密码', exact: true }).click();
    await expectVisible(page.getByRole('heading', { name: '修改密码', exact: true }), '工作台改密弹层');
    const passwordDialog = page.locator('.accessible-dialog-default');
    await expectSolidProductDialog(passwordDialog, '工作台改密弹层');
    await expectNoHoverLift(page.getByRole('button', { name: '保存新密码', exact: true }), page, '改密主按钮');
    await capture(page, '02i-workbench-change-password-dialog-1440x900.png', screenshots);
    await page.getByRole('button', { name: '关闭', exact: true }).click();

    await page.getByRole('button', { name: '删除会话：平台补贴与消费趋势', exact: true }).click();
    await expectVisible(page.getByRole('heading', { name: '删除这个会话？', exact: true }), '删除会话弹层');
    const deleteDialog = page.locator('.accessible-dialog-default');
    await expectSolidProductDialog(deleteDialog, '删除会话弹层');
    await capture(page, '02j-workbench-delete-dialog-1440x900.png', screenshots);
    await page.getByRole('button', { name: '取消', exact: true }).click();

    await page.locator('.session-select').filter({ hasText: 'AI 搜索产品观察' }).click();
    await expectVisible(page.getByRole('heading', { name: '需要你的确认', exact: true }), '结构化确认标题');
    await expectVisible(page.getByText('prepare_topic_research', { exact: true }), '确认操作工具名');
    await expectVisible(page.getByText('保存本账号对可验证来源的偏好', { exact: true }), '确认操作目的');
    await expectVisible(page.getByText('优先呈现可追溯到原始链接的研究结论', { exact: true }), '确认记忆内容');
    await expectVisible(page.getByRole('button', { name: '拒绝全部', exact: true }), '批次拒绝按钮');
    await expectVisible(page.getByRole('button', { name: '批准全部并继续', exact: true }), '批次批准按钮');
    await expectVisible(page.getByRole('button', { name: '取消本次运行', exact: true }), '取消运行按钮');
    await expectAxeClean(page, 'structured-interrupt');
    await capture(page, '02k-workbench-interrupt-1440x900.png', screenshots);
    await page.locator('.session-select').filter({ hasText: '平台补贴与消费趋势' }).click();

    const managerTrigger = page.getByRole('button', { name: '内容账号', exact: true });
    await managerTrigger.click();
    const managerHeading = page.getByRole('heading', { name: '配置你的 ContentAI', exact: true });
    const manager = page.locator('.agent-manager');
    const managerDialog = page.locator('.accessible-dialog-fullscreen');
    const rootShell = page.locator('#app');
    await expectVisible(managerHeading, '内容账号配置');
    assert.equal(await rootShell.getAttribute('inert'), '', '最外层弹窗打开后根页面应 inert');
    assert.equal(await rootShell.getAttribute('aria-hidden'), 'true', '最外层弹窗打开后根页面应隐藏于辅助技术');
    await expectSolidProductDialog(managerDialog, '内容账号弹层');
    await expectSolidProductDialog(manager, '内容账号弹层主体');

    await page.setViewportSize({ width: 360, height: 800 });
    await expectVisible(page.locator('.agent-directory'), '360px 内容账号目录');
    await expectHidden(page.locator('.agent-editor'), '360px 初始编辑器');
    await expectNoHorizontalOverflow(managerDialog, '360px 内容账号弹层');
    await expectNoHorizontalOverflow(manager, '360px 内容账号主体');
    await expectAxeClean(page, 'agent-directory');
    await expectMinimumTouchTargets(manager, '360x800 内容账号目录');
    await capture(page, '03a-agent-manager-360x800-directory.png', screenshots);

    await page.locator('.agent-directory-row').filter({ hasText: agent.name }).click();
    const accountName = page.locator('#agent-name');
    await expectVisible(accountName, '360px 账号名称字段');
    assert.equal(await accountName.inputValue(), agent.name);
    await expectHidden(page.locator('.agent-directory'), '360px 编辑态目录');
    const tabs = page.getByRole('tab');
    assert.equal(await tabs.count(), 4, '账号编辑器应提供四个标准 tab');
    const basicTab = page.getByRole('tab', { name: '基础信息', exact: true });
    const sourcesTab = page.getByRole('tab', { name: '热点来源', exact: true });
    const contentTab = page.getByRole('tab', { name: '内容规则', exact: true });
    assert.equal(await page.getByRole('tablist').getAttribute('aria-orientation'), 'horizontal', '紧凑布局应声明水平 tabs');
    assert.equal(await basicTab.getAttribute('aria-selected'), 'true');
    assert.equal(await basicTab.getAttribute('tabindex'), '0');
    assert.equal(await contentTab.getAttribute('tabindex'), '-1');
    await basicTab.focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await sourcesTab.getAttribute('aria-selected'), 'true', 'ArrowRight 应切换到下一个 tab');
    assert.equal(await sourcesTab.evaluate((element) => element === document.activeElement), true, '切换后的 tab 应获得焦点');
    const hiddenNamePanel = page.locator('#manager-panel-basic');
    assert.equal(await hiddenNamePanel.getAttribute('hidden'), '', '非活动 panel 应使用 hidden');
    const hiddenNameInput = hiddenNamePanel.locator('input');
    await hiddenNameInput.evaluate((element) => element.focus());
    assert.equal(await hiddenNameInput.evaluate((element) => element === document.activeElement), false, '隐藏 panel 的字段不得进入焦点序列');
    await page.keyboard.press('End');
    assert.equal(await contentTab.getAttribute('aria-selected'), 'true', 'End 应切换到最后一个 tab');
    await page.keyboard.press('Home');
    assert.equal(await basicTab.getAttribute('aria-selected'), 'true', 'Home 应切换到第一个 tab');
    await page.keyboard.press('ArrowRight');
    await expectSingleVerticalScrollContainer(manager, '360px 账号编辑器');
    await expectInsideViewport(page.locator('.editor-footer'), page, '360px 底部操作栏');
    await expectNoHorizontalOverflow(page.locator('.manager-layout'), '360px 账号管理布局');
    await expectAxeClean(page, 'agent-editor');
    await expectMinimumTouchTargets(manager, '360x800 内容账号编辑器');
    await basicTab.click();
    await accountName.fill('');
    await page.getByRole('button', { name: '保存内容账号', exact: true }).click();
    assert.equal(await accountName.getAttribute('aria-invalid'), 'true', '空账号名称应声明 aria-invalid');
    assert.equal(await accountName.getAttribute('aria-describedby'), 'agent-name-error', '空账号名称应关联错误说明');
    await expectFocused(accountName, '保存失败后应聚焦首个无效字段');
    await accountName.fill(agent.name);
    await capture(page, '03b-agent-manager-360x800-editor.png', screenshots);

    const directorySearch = page.getByRole('searchbox', { name: '搜索内容账号', exact: true });
    await basicTab.click();
    await page.getByRole('button', { name: '返回账号目录', exact: true }).click();
    await expectVisible(page.locator('.agent-directory'), '无草稿返回账号目录');
    const cleanDirectoryFocusRestored = await directorySearch.evaluate((element) => element === document.activeElement);
    await page.locator('.agent-directory-row').filter({ hasText: agent.name }).click();
    await expectVisible(accountName, '再次进入账号编辑器');
    await accountName.fill(`${agent.name}（草稿）`);
    await page.getByRole('button', { name: '关闭内容账号管理', exact: true }).click();
    await expectVisible(page.getByRole('heading', { name: '放弃未保存的更改？', exact: true }), '嵌套未保存确认');
    const dialogLayers = page.locator('.modal-backdrop');
    assert.equal(await dialogLayers.count(), 2, '嵌套确认应产生两层弹窗');
    assert.deepEqual(await dialogLayers.evaluateAll((elements) => elements.map((element) => element.getAttribute('data-dialog-layer'))), ['1', '2']);
    const dialogZIndexes = await dialogLayers.evaluateAll((elements) => elements.map((element) => Number(getComputedStyle(element).zIndex)));
    assert.equal(await dialogLayers.first().getAttribute('inert'), '', '嵌套确认打开后外层弹窗应 inert');
    assert.equal(await dialogLayers.last().evaluate((element) => element.contains(document.activeElement)), true, '焦点应进入顶层确认弹窗');
    await page.keyboard.press('Escape');
    await expectHidden(page.getByRole('heading', { name: '放弃未保存的更改？', exact: true }), 'Escape 只关闭顶层确认');
    await expectVisible(managerHeading, 'Escape 后外层账号弹窗仍保留');
    assert.equal(await dialogLayers.first().getAttribute('inert'), null, '顶层关闭后外层弹窗应恢复交互');
    assert.equal(await rootShell.getAttribute('inert'), '', '顶层关闭后根页面仍应 inert');

    await page.getByRole('button', { name: '关闭内容账号管理', exact: true }).click();
    await expectVisible(page.getByRole('heading', { name: '放弃未保存的更改？', exact: true }), '再次打开未保存确认');
    await dialogLayers.last().click({ position: { x: 4, y: 4 } });
    await expectHidden(page.getByRole('heading', { name: '放弃未保存的更改？', exact: true }), '遮罩关闭顶层确认');

    await page.getByRole('button', { name: '返回账号目录', exact: true }).click();
    await page.getByRole('button', { name: '放弃并返回目录', exact: true }).click();
    await expectVisible(page.locator('.agent-directory'), '放弃草稿后返回账号目录');
    const dirtyDirectoryFocusRestored = await directorySearch.evaluate((element) => element === document.activeElement);
    assert.equal(await dialogLayers.count(), 1, '返回目录后只保留外层弹窗');
    assert.equal(await rootShell.getAttribute('inert'), '', '返回目录后根页面仍应 inert');

    await page.locator('.agent-directory-row').filter({ hasText: agent.name }).click();
    await expectVisible(accountName, '第三次进入账号编辑器');
    await accountName.fill(`${agent.name}（关闭草稿）`);
    await page.getByRole('button', { name: '关闭内容账号管理', exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.getByRole('button', { name: '放弃并关闭', exact: true }).click();
    await expectHidden(managerHeading, '放弃草稿并关闭外层账号弹窗');
    assert.equal(await dialogLayers.count(), 0, '放弃并关闭后不应残留 backdrop');
    assert.equal(await rootShell.getAttribute('inert'), null, '根页面应在最外层弹窗关闭后恢复');
    assert.equal(await rootShell.getAttribute('aria-hidden'), null, '根页面辅助技术状态应恢复');
    assert.equal(await managerTrigger.evaluate((element) => element === document.activeElement), true, '外层弹窗关闭后焦点应归还触发器');
    assert.equal(cleanDirectoryFocusRestored, true, '无草稿返回目录后焦点应进入搜索框');
    assert.equal(dirtyDirectoryFocusRestored, true, '放弃草稿返回目录后焦点应进入搜索框');
    assert.ok(dialogZIndexes[1] > dialogZIndexes[0], `顶层弹窗 z-index 应更高：${JSON.stringify(dialogZIndexes)}`);
    await expectProductDrawerRadii(page);

    await page.setViewportSize({ width: 768, height: 1024 });
    await managerTrigger.click();
    await expectVisible(page.locator('.agent-directory'), '768px 内容账号目录');
    await page.locator('.agent-directory-row').filter({ hasText: agent.name }).click();
    await expectVisible(accountName, '768px 账号编辑器');
    await sourcesTab.click();
    await expectSingleVerticalScrollContainer(manager, '768px 账号编辑器');
    await expectNoHorizontalOverflow(manager, '768px 内容账号主体');
    await capture(page, '03c-agent-manager-768x1024-editor.png', screenshots);
    await page.keyboard.press('Escape');
    await expectHidden(managerHeading, '关闭 768px 内容账号弹窗');

    await page.setViewportSize({ width: 1280, height: 800 });
    await managerTrigger.click();
    await expectVisible(accountName, '1280px 账号名称字段');
    assert.equal(await page.getByRole('tablist').getAttribute('aria-orientation'), 'vertical', '三栏布局应声明纵向 tabs');
    await basicTab.focus();
    await page.keyboard.press('ArrowDown');
    assert.equal(await sourcesTab.getAttribute('aria-selected'), 'true', '纵向 tabs 应使用 ArrowDown 前进');
    await page.keyboard.press('ArrowUp');
    assert.equal(await basicTab.getAttribute('aria-selected'), 'true', '纵向 tabs 应使用 ArrowUp 返回');
    const managerColumns = await page.locator('.manager-layout').evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    assert.match(managerColumns, /^280px 184px /, `1280px 账号配置应保持三栏：${managerColumns}`);
    state.agentDetailResponses.push(
      { delay: 280, name: '过期慢响应账号' },
      { delay: 20, name: agent.name }
    );
    const financeDirectoryRow = page.locator('.agent-directory-row').filter({ hasText: agent.name });
    await financeDirectoryRow.click();
    await financeDirectoryRow.click();
    await page.waitForTimeout(340);
    assert.equal(await accountName.inputValue(), agent.name, '过期 Agent 响应不得覆盖最新选择');
    await expectNoHoverLift(page.getByRole('button', { name: '保存内容账号', exact: true }), page, '内容账号保存按钮');
    await expectNoHorizontalOverflow(manager, '1280px 内容账号主体');
    await capture(page, '03d-agent-manager-1280x800.png', screenshots);
    assert.equal(await managerDialog.evaluate((element) => element.contains(document.activeElement)), true, '打开弹窗后焦点应进入弹窗');
    await managerDialog.evaluate((element) => {
      const focusable = Array.from(element.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((item) => item instanceof HTMLElement && item.getClientRects().length > 0);
      focusable.at(-1)?.focus();
    });
    await page.keyboard.press('Tab');
    assert.equal(
      await managerDialog.evaluate((element) => Array.from(element.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).find((item) => item instanceof HTMLElement && item.getClientRects().length > 0) === document.activeElement),
      true,
      'Tab 应在弹窗尾部回绕到首个可见控件'
    );
    await page.getByRole('button', { name: '删除', exact: true }).click();
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await expectVisible(accountName, '删除最后账号后的新建编辑器');
    await page.waitForFunction(() => {
      const input = document.querySelector('#agent-name');
      return input instanceof HTMLInputElement && input.value === '';
    });
    assert.equal(await accountName.inputValue(), '', '删除最后账号后应进入空白新建状态');
    assert.equal(await accountName.evaluate((element) => element === document.activeElement), true, '删除最后账号后焦点应进入账号名称');
    await capture(page, '03e-agent-manager-1280x800-empty-after-delete.png', screenshots);
    await page.keyboard.press('Escape');
    await expectHidden(managerHeading, '关闭 1280px 内容账号弹窗');
    await expectVisible(page.locator('.onboarding-empty'), '删除最后账号后的工作台空态');
    await expectAxeClean(page, 'workbench-empty');

    await page.getByRole('button', { name: '管理后台', exact: true }).click();
    await page.waitForURL('**/admin/users');
    await expectVisible(page.getByRole('heading', { name: '用户', exact: true }), '管理后台标题');
    await expectAxeClean(page, 'admin-user-list');
    await expectMinimumTouchTargets(page.locator('.admin-list-panel'), '1440x900 后台用户列表');
    const userRow = page.locator('.admin-table tbody tr').filter({ hasText: targetUser.email });
    await expectVisible(userRow, '用户表格行');
    await userRow.click();
    await expectVisible(page.getByRole('heading', { name: targetUser.email, exact: true }), '用户详情');
    await expectVisible(page.getByRole('button', { name: /查看会话记录/ }), '会话审计入口');
    await expectAxeClean(page, 'admin-user-detail');
    await expectMinimumTouchTargets(page.locator('.admin-detail-panel'), '1440x900 后台用户详情');
    await capture(page, '04-admin-detail-1440x900.png', screenshots);

    await page.getByRole('button', { name: /查看会话记录/ }).click();
    await expectVisible(page.getByRole('heading', { name: `${targetUser.email} 的会话记录`, exact: true }), '会话审计窗口');
    await expectVisible(page.getByText(adminSessionSummary.title, { exact: true }).first(), '审计会话标题');
    await expectVisible(page.getByText('今天值得跟进的是平台补贴、消费品牌财报和 AI 搜索产品更新。', { exact: true }), '审计消息');
    await page.getByRole('button', { name: '加载更多会话', exact: true }).click();
    await expectVisible(page.getByText(olderAdminSession.title, { exact: true }), '追加的审计会话');
    await page.getByRole('button', { name: '加载更多消息', exact: true }).click();
    await expectVisible(page.getByText('补充加载的审计消息。', { exact: true }), '追加的审计消息');
    await expectAxeClean(page, 'audit-transcript');
    await capture(page, '05-admin-audit-1440x900.png', screenshots);

    await page.goto(`${baseUrl}/admin/models`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '模型管理', exact: true }), '模型管理标题');
    await expectVisible(page.locator('.model-status-panel').getByText('gpt-4.1-mini', { exact: true }), '当前模型');
    await page.getByRole('button', { name: '刷新模型', exact: true }).click();
    await expectVisible(page.getByText('已刷新 3 个可用模型。', { exact: true }), '模型刷新反馈');
    await expectAxeClean(page, 'model-config');
    await expectMinimumTouchTargets(page.locator('.model-admin-workspace'), '1440x900 模型配置');
    await capture(page, '06-admin-models-1440x900.png', screenshots);

    const modelBaseUrl = page.getByRole('textbox', { name: /Base URL/ });
    const modelName = page.getByRole('combobox', { name: '模型 ID' });
    await modelBaseUrl.fill('https://draft.example.test/v1');
    await modelName.fill('custom-model-draft');
    await page.getByRole('button', { name: '测试连接', exact: true }).click();
    await expectVisible(page.getByText(/当前模型尚未完成推理验证/), '未验证模型反馈');
    await page.getByRole('button', { name: '保存并启用', exact: true }).click();
    await expectVisible(page.getByText(/配置已被其他管理员更新/), '并发冲突反馈');
    assert.equal(await modelBaseUrl.inputValue(), 'https://draft.example.test/v1', '冲突后应保留 Base URL 草稿');
    assert.equal(await modelName.inputValue(), 'custom-model-draft', '冲突后应保留模型草稿');
    await expectVisible(page.getByText('v8', { exact: true }).first(), '冲突后同步的配置版本');
    assert.deepEqual(state.modelPutPayloads, [{
      base_url: 'https://draft.example.test/v1',
      model_name: 'custom-model-draft',
      expected_version: 7
    }], '更新已有配置时空 Key 不应进入请求');

    state.modelLoadFailure = true;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByText('无法加载当前模型配置', { exact: true }), '模型配置加载错误');
    await expectHidden(page.getByRole('button', { name: '保存并启用', exact: true }), '加载失败时保存入口');
    state.modelLoadFailure = false;
    await page.getByRole('button', { name: '重新加载', exact: true }).click();
    await expectVisible(page.getByRole('button', { name: '保存并启用', exact: true }), '重新加载后的保存入口');

    assert.deepEqual(state.unexpectedApiCalls, [], `存在未模拟 API：${state.unexpectedApiCalls.join(', ')}`);
    assert.deepEqual(state.pageErrors, [], `页面脚本错误：${state.pageErrors.join(' | ')}`);
    assert.deepEqual(state.failedResponses, [], `存在失败响应：${state.failedResponses.join(' | ')}`);
    assert.deepEqual(state.failedRequests, [], `存在失败请求：${state.failedRequests.join(' | ')}`);
    assert.ok(state.apiCalls.some((call) => call === 'POST /api/auth/login'), '未覆盖登录 API');
    assert.ok(state.apiCalls.some((call) => call.startsWith(`GET /api/admin/users/${targetUser.id}/sessions`)), '未覆盖后台会话列表 API');
    assert.ok(state.apiCalls.some((call) => call === `GET /api/admin/sessions/${adminSessionSummary.session_id}`), '未覆盖会话审计 API');

    assert.ok(state.apiCalls.some((call) => call === 'GET /api/admin/model-config'), '未覆盖模型配置 API');
    assert.ok(state.apiCalls.some((call) => call === 'POST /api/admin/model-config/probe'), '未覆盖模型探测 API');
    assert.ok(state.apiCalls.some((call) => call === 'PUT /api/admin/model-config'), '未覆盖模型并发更新 API');

    return { screenshots, apiCalls: state.apiCalls, authRegisterLayout };
  } catch (error) {
    const diagnosticPath = resolve(outputDir, 'verify-web-failure.png');
    await page.screenshot({ path: diagnosticPath, animations: 'disabled' }).catch(() => {});
    const bodyText = await page.locator('body').innerText().catch(() => '');
    console.error(`[verify:web] 当前页面：${page.url()}`);
    console.error(`[verify:web] 页面文本：${bodyText.slice(0, 1200)}`);
    console.error(`[verify:web] 页面错误：${state.pageErrors.join(' | ') || '无'}`);
    console.error(`[verify:web] 控制台错误：${state.consoleErrors.join(' | ') || '无'}`);
    console.error(`[verify:web] 失败响应：${state.failedResponses.join(' | ') || '无'}`);
    console.error(`[verify:web] 失败请求：${state.failedRequests.join(' | ') || '无'}`);
    console.error(`[verify:web] API 请求：${state.apiCalls.join(', ') || '无'}`);
    const html = await page.content().catch(() => '');
    console.error(`[verify:web] 页面 HTML：${html.slice(0, 2400)}`);
    console.error(`[verify:web] 失败截图：${diagnosticPath}`);
    throw error;
  } finally {
    await context.close();
  }
}

async function runResponsiveAdminAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 360, height: 800 },
    colorScheme: 'light',
    reducedMotion: 'reduce',
    deviceScaleFactor: 1,
    locale: 'zh-CN'
  });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseUrl });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  page.setDefaultNavigationTimeout(20000);
  const state = {
    authenticated: true,
    apiCalls: [],
    unexpectedApiCalls: [],
    pageErrors: [],
    consoleErrors: [],
    expectedConsoleResourceErrors: 0,
    failedResponses: [],
    failedRequests: [],
    mediaRequests: [],
    modelVersion: 7,
    modelLoadFailure: false,
    modelPutPayloads: [],
    agentDeleted: false,
    temporaryPasswordCount: 0,
    expectedFailedResponses: [],
    temporaryPasswordDelays: {},
    temporaryPasswordSecrets: {},
    temporaryPasswordFailureUserId: null,
    adminUserDetailDelays: {},
    adminUserDetailFailureUserId: null,
    adminStatusFailureUserId: null,
    adminRoleFailureUserId: null,
    adminSessionDelays: {},
    adminSessionFailureId: null
  };
  const screenshots = [];
  page.on('pageerror', (error) => state.pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    if (
      state.expectedConsoleResourceErrors > 0
      && message.text().startsWith('Failed to load resource: the server responded with a status of')
    ) {
      state.expectedConsoleResourceErrors -= 1;
      return;
    }
    state.consoleErrors.push(message.text());
  });
  page.on('response', (response) => {
    if (response.status() < 400) return;
    const signature = `${response.status()} ${response.request().method()} ${new URL(response.url()).pathname}`;
    const expectedIndex = state.expectedFailedResponses.indexOf(signature);
    if (expectedIndex >= 0) {
      state.expectedFailedResponses.splice(expectedIndex, 1);
      return;
    }
    state.failedResponses.push(`${response.status()} ${response.url()}`);
  });
  page.on('requestfailed', (request) => {
    state.failedRequests.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText ?? 'unknown'}`);
  });
  await installApiMocks(page, state);

  async function expectResponsiveShell(width, label) {
    await expectVisible(page.getByRole('link', { name: '用户管理', exact: true }), `${label} 用户导航`);
    await expectVisible(page.getByRole('link', { name: '模型管理', exact: true }), `${label} 模型导航`);
    await expectVisible(page.getByRole('button', { name: '返回工作台', exact: true }), `${label} 返回工作台`);
    await expectVisible(page.getByRole('button', { name: '退出管理后台', exact: true }), `${label} 退出`);
    await expectNoHorizontalOverflow(page.locator('html'), `${label} 管理后台整页`);
    await expectPlainAdminSurfaces(page, label);
    assert.equal(await page.evaluate(() => window.innerWidth), width, `${label} 视口宽度`);
  }

  async function openCompactUserDetail(width, height) {
    await page.setViewportSize({ width, height });
    await page.goto(`${baseUrl}/admin/users`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '用户', exact: true }), `${width}px 管理后台标题`);
    await expectResponsiveShell(width, `${width}px`);
    await expectVisible(page.locator('.admin-list-panel'), `${width}px 用户列表`);
    await expectHidden(page.locator('.admin-detail-panel'), `${width}px 初始用户详情`);
    if (width === 360) {
      await expectMinimumTouchTargets(page.locator('.admin-list-panel'), '360x800 后台用户列表');
    }
    const row = page.locator('.admin-table tbody tr').filter({ hasText: targetUser.email });
    await row.click();
    const back = page.getByRole('button', { name: '返回用户列表', exact: true });
    await expectVisible(back, `${width}px 用户详情返回`);
    await expectHidden(page.locator('.admin-list-panel'), `${width}px 详情态用户列表`);
    await expectVisible(page.getByRole('heading', { name: targetUser.email, exact: true }), `${width}px 用户详情`);
    await expectFocused(back, `${width}px 详情焦点应进入返回按钮`);
    await expectNoHorizontalOverflow(page.locator('.admin-workspace'), `${width}px 用户详情工作区`);
    if (width === 360) {
      await expectMinimumTouchTargets(page.locator('.admin-detail-panel'), '360x800 后台用户详情');
    }
    return { row, back };
  }

  try {
    let compact = await openCompactUserDetail(360, 800);
    await capture(page, '04a-admin-users-360x800-detail.png', screenshots);

    const generatePassword = page.getByRole('button', { name: '生成临时密码', exact: true });
    state.temporaryPasswordFailureUserId = targetUser.id;
    state.expectedConsoleResourceErrors += 1;
    state.expectedFailedResponses.push(`503 POST /api/admin/users/${targetUser.id}/temporary-password`);
    await generatePassword.click();
    const detailFailureAlert = page.locator('.admin-detail-panel').getByRole('alert');
    await expectVisible(detailFailureAlert, '360px 详情操作 5xx 错误');
    assert.match(await detailFailureAlert.innerText(), /临时密码服务暂不可用/, '详情操作错误应显示在当前可见详情面板');
    await detailFailureAlert.scrollIntoViewIfNeeded();
    await capture(page, '04g-admin-users-360x800-error.png', screenshots);
    state.temporaryPasswordFailureUserId = null;

    await generatePassword.click();
    await expectVisible(page.getByRole('heading', { name: '临时密码（仅显示一次）', exact: true }), '临时密码弹窗');
    await expectVisible(page.locator('.admin-temporary-password-dialog').getByText(targetUser.email, { exact: true }), '临时密码目标邮箱');
    await expectVisible(page.getByText('one-time-secret-1', { exact: true }), '一次性临时密码');
    await expectVisible(page.getByText(/有效期至/), '临时密码到期时间');
    await page.getByRole('button', { name: '复制临时密码', exact: true }).click();
    await expectVisible(page.getByText('临时密码已复制。', { exact: true }), '临时密码复制反馈');
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'one-time-secret-1', '复制操作应写入系统剪贴板');
    await expectAxeClean(page, 'temporary-password');
    await expectMinimumTouchTargets(page.locator('.accessible-dialog-default'), '360x800 临时密码弹窗');
    await capture(page, '04b-admin-password-360x800.png', screenshots);
    await page.getByRole('button', { name: '关闭临时密码', exact: true }).click();
    await expectHidden(page.getByRole('heading', { name: '临时密码（仅显示一次）', exact: true }), '按钮关闭临时密码弹窗');
    assert.equal((await page.locator('body').innerText()).includes('one-time-secret-1'), false, '按钮关闭后页面不得保留临时密码');
    await expectFocused(generatePassword, '关闭临时密码弹窗应归还生成按钮焦点');

    await generatePassword.click();
    await expectVisible(page.getByText('one-time-secret-2', { exact: true }), 'Escape 场景临时密码');
    await page.keyboard.press('Escape');
    await expectHidden(page.getByText('one-time-secret-2', { exact: true }), 'Escape 关闭临时密码弹窗');
    assert.equal((await page.locator('body').innerText()).includes('one-time-secret-2'), false, 'Escape 关闭后页面不得保留临时密码');

    await generatePassword.click();
    await expectVisible(page.getByText('one-time-secret-3', { exact: true }), '遮罩场景临时密码');
    await page.locator('.modal-backdrop').dispatchEvent('mousedown');
    await expectHidden(page.getByText('one-time-secret-3', { exact: true }), '遮罩关闭临时密码弹窗');
    assert.equal((await page.locator('body').innerText()).includes('one-time-secret-3'), false, '遮罩关闭后页面不得保留临时密码');

    await page.evaluate(() => {
      Object.defineProperty(navigator.clipboard, 'writeText', {
        configurable: true,
        value: () => Promise.reject(new Error('verify clipboard failure'))
      });
    });
    await generatePassword.click();
    await expectVisible(page.getByText('one-time-secret-4', { exact: true }), '复制失败场景临时密码');
    await page.getByRole('button', { name: '复制临时密码', exact: true }).click();
    await expectVisible(page.getByText('复制失败，请手动选择密码后复制。', { exact: true }), '临时密码复制失败反馈');
    await page.getByRole('button', { name: '关闭临时密码', exact: true }).click();
    assert.equal((await page.locator('body').innerText()).includes('one-time-secret-4'), false, '复制失败后的关闭仍应清除临时密码');
    assert.equal((await page.locator('body').innerText()).includes('复制失败，请手动选择密码后复制。'), false, '复制失败反馈不得泄漏到其他页面状态');

    await compact.back.click();
    await expectVisible(page.locator('.admin-list-panel'), '360px 返回用户列表');
    await expectFocused(compact.row, '360px 返回列表应恢复选中用户焦点');
    await capture(page, '04c-admin-users-360x800-list.png', screenshots);

    compact = await openCompactUserDetail(768, 1024);
    await capture(page, '04d-admin-users-768x1024-detail.png', screenshots);
    await page.getByRole('button', { name: /查看会话记录/ }).click();
    await expectVisible(page.locator('.admin-audit-sessions'), '768px 会话索引');
    await expectHidden(page.locator('.admin-transcript'), '768px 初始 transcript');
    await expectAxeClean(page, 'audit-list');
    await expectMinimumTouchTargets(page.locator('.admin-audit-window'), '768x1024 审计列表');
    const sessionTarget = page.locator('[data-session-id="admin-session-17"]');
    state.adminSessionFailureId = 'admin-session-17';
    state.expectedConsoleResourceErrors += 2;
    state.expectedFailedResponses.push(
      '502 GET /api/admin/sessions/admin-session-17',
      '502 GET /api/admin/sessions/admin-session-17/messages'
    );
    const savedScrollTop = await sessionTarget.evaluate((element) => {
      const list = element.closest('.admin-audit-sessions');
      list.scrollTop = list.scrollHeight;
      const top = list.scrollTop;
      element.click();
      return top;
    });
    assert.ok(savedScrollTop > 0, `会话索引应可滚动：${savedScrollTop}`);
    const auditBack = page.getByRole('button', { name: '返回会话列表', exact: true });
    await expectVisible(auditBack, '768px transcript 返回');
    await expectHidden(page.locator('.admin-audit-sessions'), '768px transcript 态会话索引');
    await expectVisible(page.locator('.admin-transcript'), '768px transcript');
    await expectFocused(auditBack, '768px transcript 焦点应进入返回按钮');
    const auditFailureAlert = page.locator('.admin-audit-window').getByRole('alert');
    await expectVisible(auditFailureAlert, '768px 当前审计状态 5xx 错误');
    assert.match(await auditFailureAlert.innerText(), /会话.+暂不可用/, '审计错误应显示在当前可见 transcript');
    await capture(page, '05c-admin-audit-768x1024-error.png', screenshots);
    state.adminSessionFailureId = null;
    await page.getByRole('button', { name: '重试加载会话', exact: true }).click();
    await expectVisible(page.getByText(auditLongContent, { exact: true }), '长中文、URL 与无空格内容');
    state.adminSessionFailureId = 'admin-session-17';
    state.expectedConsoleResourceErrors += 1;
    state.expectedFailedResponses.push('502 GET /api/admin/sessions/admin-session-17/messages');
    await page.getByRole('button', { name: '加载更多消息', exact: true }).click();
    const messagePaginationAlert = page.locator('.admin-audit-window').getByRole('alert');
    await expectVisible(messagePaginationAlert, '768px 消息翻页 5xx 错误');
    assert.match(await messagePaginationAlert.innerText(), /会话消息暂不可用/, '消息翻页错误应显示在当前 transcript');
    state.adminSessionFailureId = null;
    await page.getByRole('button', { name: '重试加载消息', exact: true }).click();
    await expectVisible(page.getByText('补充加载的审计消息。', { exact: true }), '消息翻页失败后的恢复路径');
    await expectNoHorizontalOverflow(page.locator('.admin-audit-window'), '768px 会话审计窗口');
    await expectNoHorizontalOverflow(page.locator('.admin-session-transcript'), '768px transcript');
    await expectPlainAdminSurfaces(page, '768px 会话 transcript');
    await capture(page, '05a-admin-audit-768x1024-transcript.png', screenshots);
    await auditBack.click();
    await expectVisible(page.locator('.admin-audit-sessions'), '768px 返回会话索引');
    await expectHidden(page.locator('.admin-transcript'), '768px 返回后 transcript');
    const restoredScrollTop = await page.locator('.admin-audit-sessions').evaluate((element) => element.scrollTop);
    assert.ok(Math.abs(restoredScrollTop - savedScrollTop) <= 2, `返回会话索引应恢复 scrollTop：${JSON.stringify({ savedScrollTop, restoredScrollTop })}`);
    await expectFocused(sessionTarget, '返回会话索引应恢复选中会话焦点');
    assert.equal(await sessionTarget.getAttribute('aria-current'), 'page', '返回会话索引应保留选中状态');
    await capture(page, '05b-admin-audit-768x1024-list.png', screenshots);
    await page.getByRole('button', { name: '关闭会话审计', exact: true }).click();

    compact = await openCompactUserDetail(1024, 768);
    await capture(page, '04e-admin-users-1024x768-detail.png', screenshots);
    await compact.back.click();

    for (const viewport of [
      { width: 360, height: 800 },
      { width: 768, height: 1024 },
      { width: 1024, height: 768 }
    ]) {
      await page.setViewportSize(viewport);
      await page.goto(`${baseUrl}/admin/models`, { waitUntil: 'domcontentloaded' });
      await expectVisible(page.getByRole('heading', { name: '模型管理', exact: true }), `${viewport.width}px 模型管理标题`);
      await expectResponsiveShell(viewport.width, `${viewport.width}px`);
      const summary = page.locator('.model-status-summary');
      const form = page.locator('.model-config-panel');
      const details = page.locator('.model-status-details');
      await expectVisible(summary, `${viewport.width}px 当前状态摘要`);
      await expectVisible(form, `${viewport.width}px 模型配置表单`);
      await expectVisible(details, `${viewport.width}px 完整状态 details`);
      await expectHidden(page.locator('.model-status-panel'), `${viewport.width}px 桌面状态栏`);
      if (viewport.width === 360) {
        await expectMinimumTouchTargets(page.locator('.model-admin-workspace'), '360x800 模型配置');
      }
      const positions = await Promise.all([summary, form, details].map((locator) => locator.evaluate((element) => element.getBoundingClientRect().top)));
      assert.ok(positions[0] < positions[1] && positions[1] < positions[2], `${viewport.width}px 模型窄屏顺序错误：${positions.join(' < ')}`);
      await expectHidden(details.locator('.model-status-content'), `${viewport.width}px 默认折叠完整状态`);
      await page.locator('.model-admin-workspace').evaluate((element) => { element.scrollTop = 0; });
      await capture(page, `06a-admin-models-${viewport.width}x${viewport.height}.png`, screenshots);
      await details.locator('summary').click();
      await expectVisible(details.locator('.model-status-content'), `${viewport.width}px 展开的完整状态`);
      await expectNoHorizontalOverflow(page.locator('html'), `${viewport.width}px 模型管理整页`);
      if (viewport.width === 768) {
        await details.scrollIntoViewIfNeeded();
        await capture(page, '06a-admin-models-768x1024-details.png', screenshots);
      }
    }

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`${baseUrl}/admin/users`, { waitUntil: 'domcontentloaded' });
    await expectResponsiveShell(1280, '1280px');
    await expectVisible(page.locator('.admin-list-panel'), '1280px 用户列表');
    await expectVisible(page.locator('.admin-detail-panel'), '1280px 用户详情栏');
    await expectHidden(page.getByRole('button', { name: '返回用户列表', exact: true }), '1280px 紧凑返回按钮');
    const targetUserRow = page.locator('.admin-table tbody tr').filter({ hasText: targetUser.email });
    const reviewerUserRow = page.locator('.admin-table tbody tr').filter({ hasText: reviewerUser.email });
    state.adminUserDetailDelays[targetUser.id] = 280;
    state.adminUserDetailDelays[reviewerUser.id] = 20;
    await targetUserRow.click();
    await reviewerUserRow.click();
    await expectVisible(page.getByRole('heading', { name: reviewerUser.email, exact: true }), '1280px 快速选择的用户详情');
    await page.waitForTimeout(340);
    await expectVisible(page.getByRole('heading', { name: reviewerUser.email, exact: true }), '慢响应不得覆盖快速用户选择');
    await expectHidden(page.getByRole('heading', { name: targetUser.email, exact: true }), '过期用户详情不得重新显示');
    state.adminUserDetailDelays[targetUser.id] = 0;
    state.adminUserDetailDelays[reviewerUser.id] = 0;

    await targetUserRow.click();
    await expectVisible(page.getByRole('heading', { name: targetUser.email, exact: true }), '1280px 用户详情');
    const userColumns = await page.locator('.admin-workspace').evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    assert.match(userColumns, /\d+px \d+px/, `1280px 用户页应为双栏：${userColumns}`);
    await capture(page, '04f-admin-users-1280x800.png', screenshots);

    await page.getByRole('button', { name: /查看会话记录/ }).click();
    await expectVisible(page.locator('.admin-audit-sessions'), '1280px 会话索引');
    state.adminSessionDelays['admin-session-2'] = 280;
    state.adminSessionDelays['admin-session-3'] = 20;
    await page.locator('[data-session-id="admin-session-2"]').click();
    await page.locator('[data-session-id="admin-session-3"]').click();
    await expectVisible(page.getByRole('heading', { name: '审计会话 03', exact: true }), '快速会话 B');
    await page.waitForTimeout(340);
    await expectVisible(page.getByRole('heading', { name: '审计会话 03', exact: true }), '慢会话 A 不得覆盖 B');
    assert.equal(await page.locator('.admin-transcript').getAttribute('aria-busy'), 'false', '过期会话 finally 不得污染当前 loading');
    state.adminSessionDelays['admin-session-2'] = 0;
    state.adminSessionDelays['admin-session-3'] = 280;
    await page.getByRole('button', { name: '加载更多消息', exact: true }).click();
    await page.locator('[data-session-id="admin-session-4"]').click();
    await expectVisible(page.getByRole('heading', { name: '审计会话 04', exact: true }), '消息翻页中切换到会话 B');
    await page.waitForTimeout(340);
    assert.equal(await page.locator('.admin-transcript').getAttribute('aria-busy'), 'false', '切换会话必须使旧消息翻页 finally 与 loading 失效');
    state.adminSessionDelays['admin-session-3'] = 0;
    await page.getByRole('button', { name: '关闭会话审计', exact: true }).click();

    state.temporaryPasswordDelays[targetUser.id] = 280;
    state.temporaryPasswordSecrets[targetUser.id] = 'stale-secret-for-creator';
    await generatePassword.click();
    await reviewerUserRow.click();
    await expectVisible(page.getByRole('heading', { name: reviewerUser.email, exact: true }), '切换后的用户 B 详情');
    await page.waitForTimeout(340);
    await expectHidden(page.getByRole('heading', { name: '临时密码（仅显示一次）', exact: true }), '用户切换后旧临时密码弹窗');
    assert.equal((await page.locator('body').innerText()).includes('stale-secret-for-creator'), false, '用户 A 的过期密码响应不得显示或保留');
    await expectVisible(page.getByText('上一位用户的临时密码请求已取消，未显示任何密码。', { exact: true }), '过期密码响应的非敏感提示');
    await capture(page, '04h-admin-users-1280x800-stale-password.png', screenshots);

    state.adminStatusFailureUserId = reviewerUser.id;
    state.expectedConsoleResourceErrors += 1;
    state.expectedFailedResponses.push(`503 POST /api/admin/users/${reviewerUser.id}/disable`);
    await page.getByRole('button', { name: '禁用用户', exact: true }).click();
    const statusDialog = page.locator('.accessible-dialog-default').filter({ hasText: '禁用这个用户？' });
    await statusDialog.getByRole('button', { name: '确认禁用', exact: true }).click();
    const statusFailureAlert = statusDialog.getByRole('alert');
    await expectVisible(statusFailureAlert, '用户启停确认弹窗 5xx 错误');
    assert.match(await statusFailureAlert.innerText(), /用户状态服务暂不可用/, '用户启停错误必须显示在当前确认弹窗');
    state.adminStatusFailureUserId = null;
    await statusDialog.getByRole('button', { name: '确认禁用', exact: true }).click();
    await expectHidden(statusDialog, '用户启停重试成功后关闭确认弹窗');

    state.adminRoleFailureUserId = reviewerUser.id;
    state.expectedConsoleResourceErrors += 1;
    state.expectedFailedResponses.push(`503 PATCH /api/admin/users/${reviewerUser.id}`);
    await page.getByRole('button', { name: '降级为普通用户', exact: true }).click();
    const roleDialog = page.locator('.accessible-dialog-default').filter({ hasText: '降级这个管理员？' });
    await roleDialog.getByRole('button', { name: '确认修改角色', exact: true }).click();
    const roleFailureAlert = roleDialog.getByRole('alert');
    await expectVisible(roleFailureAlert, '用户角色确认弹窗 5xx 错误');
    assert.match(await roleFailureAlert.innerText(), /用户角色服务暂不可用/, '用户角色错误必须显示在当前确认弹窗');
    state.adminRoleFailureUserId = null;
    await roleDialog.getByRole('button', { name: '确认修改角色', exact: true }).click();
    await expectHidden(roleDialog, '用户角色重试成功后关闭确认弹窗');

    await page.goto(`${baseUrl}/admin/models`, { waitUntil: 'domcontentloaded' });
    await expectResponsiveShell(1280, '1280px 模型页');
    await expectHidden(page.locator('.model-status-summary'), '1280px 窄屏状态摘要');
    await expectHidden(page.locator('.model-status-details'), '1280px 窄屏完整状态');
    await expectVisible(page.locator('.model-config-panel'), '1280px 模型配置表单');
    await expectVisible(page.locator('.model-status-panel'), '1280px 桌面状态栏');
    const modelColumns = await page.locator('.model-admin-workspace').evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    assert.match(modelColumns, /\d+px \d+px/, `1280px 模型页应为双栏：${modelColumns}`);
    await expectNoHorizontalOverflow(page.locator('html'), '1280px 模型管理整页');
    await capture(page, '06b-admin-models-1280x800.png', screenshots);

    assert.deepEqual(state.unexpectedApiCalls, [], `响应式后台存在未模拟 API：${state.unexpectedApiCalls.join(', ')}`);
    assert.deepEqual(state.pageErrors, [], `响应式后台脚本错误：${state.pageErrors.join(' | ')}`);
    assert.deepEqual(state.consoleErrors, [], `响应式后台控制台错误：${state.consoleErrors.join(' | ')}`);
    assert.equal(state.expectedConsoleResourceErrors, 0, '响应式后台预期控制台资源错误数量不匹配');
    assert.deepEqual(state.failedResponses, [], `响应式后台存在失败响应：${state.failedResponses.join(' | ')}`);
    assert.deepEqual(state.expectedFailedResponses, [], `响应式后台预期失败响应未发生：${state.expectedFailedResponses.join(' | ')}`);
    assert.deepEqual(state.failedRequests, [], `响应式后台存在失败请求：${state.failedRequests.join(' | ')}`);
    return { screenshots, apiCalls: state.apiCalls };
  } finally {
    await context.close();
  }
}

async function runMotionBackdropAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    colorScheme: 'light',
    reducedMotion: 'no-preference',
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  await page.route('**/api/auth/me', (route) => fulfillJson(
    route,
    { detail: { code: 'UNAUTHENTICATED', message: '请先登录', retryable: false } },
    401
  ));
  try {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.locator('.ambient-backdrop__poster'), '动态背景的静态后备');
    assert.equal(await page.locator('.ambient-backdrop').getAttribute('data-material'), 'web-background');
    await page.locator('.ambient-backdrop__video').waitFor({ state: 'attached' });
    assert.equal(await page.locator('.ambient-backdrop__video').count(), 1, '未减少动画模式应加载本地视频节点');
    return 'web-background-local';
  } finally {
    await context.close();
  }
}

async function runConstrainedBackdropAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    colorScheme: 'light',
    reducedMotion: 'no-preference',
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  const mp4Requests = [];
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'connection', {
      configurable: true,
      value: {
        saveData: true,
        effectiveType: '4g',
        addEventListener() {},
        removeEventListener() {}
      }
    });
  });
  page.on('request', (request) => {
    if (/\.mp4(?:$|\?)/i.test(request.url())) mp4Requests.push(request.url());
  });
  await page.route('**/api/auth/me', (route) => fulfillJson(
    route,
    { detail: { code: 'UNAUTHENTICATED', message: '请先登录', retryable: false } },
    401
  ));
  try {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.locator('.ambient-backdrop__poster'), 'Save-Data 静态海报');
    assert.equal(await page.locator('.ambient-backdrop__video').count(), 0, 'Save-Data 不应创建视频节点');
    assert.deepEqual(mp4Requests, [], 'Save-Data 不应请求 MP4');
    return 'poster-only-on-save-data';
  } finally {
    await context.close();
  }
}

async function runDirectProductIsolationAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1024, height: 768 },
    colorScheme: 'light',
    reducedMotion: 'no-preference',
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  const state = {
    authenticated: true,
    apiCalls: [],
    unexpectedApiCalls: [],
    pageErrors: [],
    consoleErrors: [],
    failedResponses: [],
    failedRequests: [],
    mediaRequests: [],
    modelVersion: 7,
    modelLoadFailure: false,
    modelPutPayloads: []
  };
  page.on('request', (request) => {
    if (/\.mp4(?:$|\?)/i.test(request.url())) state.mediaRequests.push(request.url());
  });
  page.on('pageerror', (error) => state.pageErrors.push(error.message));
  await installApiMocks(page, state);
  try {
    const authenticatedRoutes = ['/app', '/change-password', '/admin/users', '/admin/models'];
    for (const route of authenticatedRoutes) {
      await page.goto(`${baseUrl}${route}`, { waitUntil: 'domcontentloaded' });
      await expectVisible(page.locator('main').first(), `直接进入认证路由 ${route}`);
      assert.equal(
        await page.locator('.ambient-backdrop').count(),
        0,
        `认证路由 ${route} 不应挂载认证背景`
      );
      assert.deepEqual(state.mediaRequests, [], `认证路由 ${route} 不应请求 MP4`);
    }
    assert.deepEqual(state.pageErrors, [], `直接进入工作台脚本错误：${state.pageErrors.join(' | ')}`);
    return {
      status: 'authenticated-routes-no-backdrop',
      routes: authenticatedRoutes,
      mp4RequestCount: state.mediaRequests.length
    };
  } finally {
    await context.close();
  }
}

async function runAuthVideoFailureAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    colorScheme: 'light',
    reducedMotion: 'no-preference',
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  await page.route('**/api/auth/me', (route) => fulfillJson(
    route,
    { detail: { code: 'UNAUTHENTICATED', message: '请先登录', retryable: false } },
    401
  ));
  await page.route(/\.mp4(?:$|\?)/i, (route) => route.abort('failed'));
  try {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '欢迎回到 ContentAI', exact: true }), '视频失败时的登录表单');
    await expectVisible(page.locator('.ambient-backdrop__poster'), '视频失败时的静态海报');
    await page.locator('.ambient-backdrop[data-video-state="failed"]').waitFor({ state: 'visible' });
    await expectMinimumTouchTargets(page.locator('.auth-form-surface'), '视频失败时的认证表单');
    return 'auth-video-failure-form-available';
  } finally {
    await context.close();
  }
}

async function runChangePasswordSurfaceAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1024, height: 768 },
    colorScheme: 'light',
    reducedMotion: 'no-preference',
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  const mp4Requests = [];
  page.on('request', (request) => {
    if (/\.mp4(?:$|\?)/i.test(request.url())) mp4Requests.push(request.url());
  });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me' && request.method() === 'GET') {
      return fulfillJson(route, { ...authUser, must_change_password: true });
    }
    return fulfillJson(route, {
      detail: { code: 'VERIFY_WEB_UNMOCKED', message: `验收脚本未模拟 ${request.method()} ${path}`, retryable: false }
    }, 501);
  });
  try {
    await page.goto(`${baseUrl}/change-password`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '修改密码', exact: true }), '强制修改密码标题');
    await expectVisible(page.locator('.password-page'), '中性修改密码页面');
    assert.equal(await page.locator('.auth-shell').count(), 0, '修改密码不应复用认证 hero');
    assert.equal(await page.locator('.ambient-backdrop').count(), 0, '修改密码不应挂载认证背景');
    assert.deepEqual(mp4Requests, [], '直接进入修改密码不应请求 MP4');
    await expectAxeClean(page, 'forced-change-password');
    await expectMinimumTouchTargets(page.locator('.password-page'), '1024x768 强制修改密码');
    const screenshot = resolve(outputDir, '07-change-password-1024x768.png');
    await page.screenshot({ path: screenshot, animations: 'disabled' });
    assert.ok(statSync(screenshot).size > 1000, `截图为空：${screenshot}`);
    return { status: 'change-password-neutral-product-surface', screenshot };
  } finally {
    await context.close();
  }
}

async function runPageScaleAcceptance(browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    colorScheme: 'light',
    reducedMotion: 'reduce',
    locale: 'zh-CN'
  });
  const page = await context.newPage();
  const state = {
    authenticated: true,
    apiCalls: [],
    unexpectedApiCalls: [],
    pageErrors: [],
    consoleErrors: [],
    failedResponses: [],
    failedRequests: [],
    mediaRequests: [],
    modelVersion: 7,
    modelLoadFailure: false,
    modelPutPayloads: [],
    agentDeleted: false,
    agentDetailResponses: []
  };
  await installApiMocks(page, state);
  const cdp = await context.newCDPSession(page);
  try {
    await page.goto(`${baseUrl}/app`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.locator('.workbench-shell'), '1280x720 工作台');
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 640,
      height: 360,
      deviceScaleFactor: 2,
      mobile: false,
      screenWidth: 1280,
      screenHeight: 720
    });
    await page.waitForFunction(() => window.innerWidth === 640 && window.devicePixelRatio === 2);
    const zoomMetrics = await page.evaluate(() => ({
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio,
      visualViewportWidth: window.visualViewport.width,
      visualViewportHeight: window.visualViewport.height
    }));
    assert.equal(zoomMetrics.innerWidth, 640, `200% browser zoom 应把 1280px 物理宽度重排为 640 CSS px：${JSON.stringify(zoomMetrics)}`);
    assert.equal(zoomMetrics.devicePixelRatio, 2, `200% browser zoom DPR 错误：${JSON.stringify(zoomMetrics)}`);
    assert.ok(
      Math.abs(zoomMetrics.visualViewportWidth - 640) <= 1,
      `200% browser zoom visual viewport 宽度错误：${JSON.stringify(zoomMetrics)}`
    );
    await expectNoHorizontalOverflow(page.locator('html'), '640x360 200% browser zoom 工作台');
    await expectInsideViewport(page.locator('.workbench-header'), page, '200% browser zoom header');
    const sessionToggle = page.locator('.session-nav-toggle');
    await expectVisible(sessionToggle, '200% browser zoom 会话菜单触发器');
    await sessionToggle.click();
    const zoomDrawer = page.locator('#session-navigation');
    await expectVisible(zoomDrawer, '200% browser zoom 会话抽屉');
    await expectInsideViewport(zoomDrawer, page, '200% browser zoom 会话抽屉');
    await expectMinimumTouchTargets(zoomDrawer, '200% browser zoom 会话抽屉');
    await page.keyboard.press('Escape');
    await expectHidden(zoomDrawer, '200% browser zoom Escape 关闭会话抽屉');
    const zoomUserMenuButton = page.locator('.user-menu-button');
    await zoomUserMenuButton.click();
    const zoomUserMenu = page.locator('#user-account-menu');
    await expectVisible(zoomUserMenu, '200% browser zoom 账号菜单');
    await expectInsideViewport(zoomUserMenu, page, '200% browser zoom 账号菜单');
    await expectMinimumTouchTargets(zoomUserMenu, '200% browser zoom 账号菜单');
    await zoomUserMenuButton.click();
    await expectHidden(zoomUserMenu, '200% browser zoom 关闭账号菜单');
    const composer = page.getByRole('textbox', { name: '对话输入', exact: true });
    await composer.fill('200% 缩放功能验证');
    assert.equal(await composer.inputValue(), '200% 缩放功能验证', '200% 缩放下输入功能应保持可用');
    await expectInsideViewport(composer, page, '200% browser zoom 对话输入');
    const sendButton = page.locator('.send-button');
    await expectVisible(sendButton, '200% browser zoom 发送按钮');
    assert.equal(await sendButton.isEnabled(), true, '200% browser zoom 发送按钮应可用');
    await expectInsideViewport(sendButton, page, '200% browser zoom 发送按钮');
    await expectMinimumTouchTargets(page.locator('.workbench-shell'), '200% browser zoom 工作台');
    const screenshot = resolve(outputDir, '08-workbench-1280x720-browser-zoom-200.png');
    await page.screenshot({ path: screenshot, animations: 'disabled' });
    assert.ok(statSync(screenshot).size > 1000, `截图为空：${screenshot}`);
    await cdp.send('Emulation.clearDeviceMetricsOverride');
    await cdp.send('Emulation.setPageScaleFactor', { pageScaleFactor: 2 });
    await page.waitForFunction(() => window.visualViewport?.scale >= 1.9);
    const visualScale = await page.evaluate(() => window.visualViewport.scale);
    assert.ok(visualScale >= 1.9, `200% pinch zoom smoke 未生效：${visualScale}`);
    assert.deepEqual(state.pageErrors, [], `200% 缩放脚本错误：${state.pageErrors.join(' | ')}`);
    return {
      status: 'browser-zoom-200-reflow-functional',
      ...zoomMetrics,
      pinchScale: visualScale,
      screenshot
    };
  } finally {
    await cdp.send('Emulation.setPageScaleFactor', { pageScaleFactor: 1 }).catch(() => {});
    await cdp.send('Emulation.clearDeviceMetricsOverride').catch(() => {});
    await cdp.detach().catch(() => {});
    await context.close();
  }
}

let server = null;
let browser = null;
let shuttingDown = false;

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  try {
    await browser?.close();
  } finally {
    await stopVite(server?.child);
  }
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    const exitCode = signal === 'SIGINT' ? 130 : 143;
    void shutdown().finally(() => process.exit(exitCode));
  });
}

try {
  server = await startVerifiedVite();
  browser = await chromium.launch({
    executablePath: chromeExecutable(),
    headless: true,
    args: ['--disable-background-networking', '--disable-component-update']
  });
  const result = await runDesktopAcceptance(browser);
  const responsiveAdmin = await runResponsiveAdminAcceptance(browser);
  const backdropMaterial = await runMotionBackdropAcceptance(browser);
  const constrainedBackdrop = await runConstrainedBackdropAcceptance(browser);
  const productIsolation = await runDirectProductIsolationAcceptance(browser);
  const authVideoFailure = await runAuthVideoFailureAcceptance(browser);
  const changePasswordSurface = await runChangePasswordSurfaceAcceptance(browser);
  const pageScaleAcceptance = await runPageScaleAcceptance(browser);
  console.log(JSON.stringify({
    ok: true,
    baseUrl,
    backdropMaterial,
    constrainedBackdrop,
    productIsolation,
    authVideoFailure,
    changePasswordSurface,
    pageScaleAcceptance,
    authRegisterLayout: result.authRegisterLayout,
    axe: {
      stateCount: axeAudits.length,
      seriousCriticalViolationCount: axeAudits.reduce(
        (count, audit) => count + audit.seriousCriticalCount,
        0
      ),
      audits: axeAudits
    },
    responsiveAdmin: {
      screenshotCount: responsiveAdmin.screenshots.length,
      screenshots: responsiveAdmin.screenshots,
      apiCalls: responsiveAdmin.apiCalls
    },
    screenshotCount: result.screenshots.length + responsiveAdmin.screenshots.length + 1,
    screenshots: [
      ...result.screenshots,
      ...responsiveAdmin.screenshots,
      pageScaleAcceptance.screenshot
    ],
    apiCalls: result.apiCalls
  }, null, 2));
} catch (error) {
  process.exitCode = 1;
  console.error('[verify:web] 桌面端验收失败');
  console.error(error instanceof Error ? error.stack ?? error.message : String(error));
  if (server?.output.length) console.error(server.output.join('').trim());
} finally {
  await shutdown();
}
