import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, rmSync, statSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
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

const adminSessionDetail = {
  ...adminSessionSummary,
  messages: [
    { id: 'audit-1', role: 'user', content: '你好' },
    { id: 'audit-2', role: 'assistant', content: '你好，我可以帮你筛选热点、梳理判断或起草内容。' },
    { id: 'audit-3', role: 'user', content: '获取热点' },
    { id: 'audit-4', role: 'assistant', content: '今天值得跟进的是平台补贴、消费品牌财报和 AI 搜索产品更新。' },
    { id: 'audit-5', role: 'user', content: '先分析平台补贴。' }
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
    if (path === '/api/agents' && method === 'GET') return fulfillJson(route, [agent]);
    if (path === `/api/agents/${agent.id}` && method === 'GET') return fulfillJson(route, agent);
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
      return fulfillJson(route, { items: [targetUser], next_cursor: null });
    }
    if (path === `/api/admin/users/${targetUser.id}` && method === 'GET') {
      return fulfillJson(route, targetUser);
    }
    if (path === `/api/admin/users/${targetUser.id}/sessions` && method === 'GET') {
      return url.searchParams.has('cursor')
        ? fulfillJson(route, { items: [olderAdminSession], next_cursor: null })
        : fulfillJson(route, { items: [adminSessionSummary], next_cursor: 'session-cursor-1' });
    }
    if (path === '/api/admin/usage' && method === 'GET') {
      return fulfillJson(route, { items: usageBuckets });
    }
    if (path === `/api/admin/sessions/${adminSessionSummary.session_id}` && method === 'GET') {
      return fulfillJson(route, adminSessionDetail);
    }
    if (path === `/api/admin/sessions/${adminSessionSummary.session_id}/messages` && method === 'GET') {
      return url.searchParams.has('cursor')
        ? fulfillJson(route, { items: [{ id: 'audit-6', role: 'assistant', message_type: 'text', content: '补充加载的审计消息。', created_at: fixedNow }], next_cursor: null })
        : fulfillJson(route, { items: adminSessionDetail.messages, next_cursor: 'message-cursor-1' });
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
    modelPutPayloads: []
  };
  const screenshots = [];
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
    await capture(page, '01-auth-login-1440x900.png', screenshots);
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
    await capture(page, '02-workbench-1440x900.png', screenshots);

    const sessionRail = page.locator('#session-navigation');
    await expectElementWidth(sessionRail, 280, '1440px 侧栏应为 280px');

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
    await expectVisible(page.getByRole('menuitem', { name: '内容账号', exact: true }), '手机内容账号入口');
    await expectVisible(page.getByRole('menuitem', { name: '管理后台', exact: true }), '手机管理后台入口');
    await expectVisible(page.getByRole('menuitem', { name: '修改密码', exact: true }), '手机改密入口');
    await expectVisible(page.getByRole('menuitem', { name: '退出登录', exact: true }), '手机退出入口');
    await page.keyboard.press('Escape');

    await page.setViewportSize({ width: 390, height: 844 });
    const mediumPhoneToggle = page.getByRole('button', { name: '打开会话导航', exact: true });
    await mediumPhoneToggle.click();
    await expectVisible(sessionRail, '390px 手机会话抽屉');
    await expectElementWidth(sessionRail, 360, '390px 手机抽屉应严格限制为 360px');
    assert.equal(await sessionRail.getAttribute('role'), 'dialog', '手机会话抽屉应使用 dialog 语义');
    assert.equal(await sessionRail.getAttribute('aria-modal'), 'true', '手机会话抽屉应声明 aria-modal');
    await expectInsideViewport(sessionRail, page, '390px 手机会话抽屉');
    await capture(page, '02h-workbench-390x844-drawer.png', screenshots);
    await page.keyboard.press('Escape');

    await page.setViewportSize({ width: 600, height: 800 });
    await page.getByRole('button', { name: '打开会话导航', exact: true }).click();
    await page.locator('.session-rail-scrim').click({ position: { x: 540, y: 400 } });
    await expectHidden(sessionRail, '遮罩关闭手机会话抽屉');

    await page.setViewportSize({ width: 1440, height: 900 });
    assert.equal(state.mediaRequests.length, 0, 'reduced-motion 登录及工作台不应请求 MP4');

    await page.locator('.user-menu-button').click();
    await page.getByRole('menuitem', { name: '修改密码', exact: true }).click();
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
    await capture(page, '02k-workbench-interrupt-1440x900.png', screenshots);
    await page.locator('.session-select').filter({ hasText: '平台补贴与消费趋势' }).click();

    await page.getByRole('button', { name: '内容账号', exact: true }).click();
    await expectVisible(page.getByRole('heading', { name: '配置你的 ContentAI', exact: true }), '内容账号配置');
    await expectVisible(page.getByRole('textbox', { name: '账号名称', exact: true }), '账号名称字段');
    await page.getByRole('textbox', { name: '账号名称', exact: true }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: '账号名称', exact: true }).inputValue(), agent.name);
    const managerColumns = await page.locator('.manager-layout').evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    assert.match(managerColumns, /^280px 184px /, `账号配置列宽不正确：${managerColumns}`);
    await expectSolidProductDialog(page.locator('.accessible-dialog-fullscreen'), '内容账号弹层');
    await expectSolidProductDialog(page.locator('.agent-manager'), '内容账号弹层主体');
    await expectNoHoverLift(page.getByRole('button', { name: '保存内容账号', exact: true }), page, '内容账号保存按钮');
    await capture(page, '03-agent-manager-1440x900.png', screenshots);
    const managerDialog = page.locator('.accessible-dialog-fullscreen');
    assert.equal(await managerDialog.evaluate((element) => element.contains(document.activeElement)), true, '打开弹窗后焦点应进入弹窗');
    await managerDialog.evaluate((element) => {
      const focusable = Array.from(element.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((item) => item instanceof HTMLElement && !item.hidden && item.getAttribute('aria-hidden') !== 'true');
      focusable.at(-1)?.focus();
    });
    await page.keyboard.press('Tab');
    assert.equal(
      await managerDialog.evaluate((element) => element.querySelector('a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])') === document.activeElement),
      true,
      'Tab 应在弹窗尾部回绕到首个控件'
    );
    await page.keyboard.press('Escape');
    await expectHidden(page.getByRole('heading', { name: '配置你的 ContentAI', exact: true }), '内容账号配置');

    await page.getByRole('button', { name: '管理后台', exact: true }).click();
    await page.waitForURL('**/admin/users');
    await expectVisible(page.getByRole('heading', { name: '用户', exact: true }), '管理后台标题');
    const userRow = page.locator('.admin-table tbody tr').filter({ hasText: targetUser.email });
    await expectVisible(userRow, '用户表格行');
    await userRow.click();
    await expectVisible(page.getByRole('heading', { name: targetUser.email, exact: true }), '用户详情');
    await expectVisible(page.getByRole('button', { name: /查看会话记录/ }), '会话审计入口');
    await capture(page, '04-admin-detail-1440x900.png', screenshots);

    await page.getByRole('button', { name: /查看会话记录/ }).click();
    await expectVisible(page.getByRole('heading', { name: `${targetUser.email} 的会话记录`, exact: true }), '会话审计窗口');
    await expectVisible(page.getByText(adminSessionSummary.title, { exact: true }).first(), '审计会话标题');
    await expectVisible(page.getByText('今天值得跟进的是平台补贴、消费品牌财报和 AI 搜索产品更新。', { exact: true }), '审计消息');
    await page.getByRole('button', { name: '加载更多会话', exact: true }).click();
    await expectVisible(page.getByText(olderAdminSession.title, { exact: true }), '追加的审计会话');
    await page.getByRole('button', { name: '加载更多消息', exact: true }).click();
    await expectVisible(page.getByText('补充加载的审计消息。', { exact: true }), '追加的审计消息');
    await capture(page, '05-admin-audit-1440x900.png', screenshots);

    await page.goto(`${baseUrl}/admin/models`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '模型管理', exact: true }), '模型管理标题');
    await expectVisible(page.getByText('gpt-4.1-mini', { exact: true }).first(), '当前模型');
    await page.getByRole('button', { name: '刷新模型', exact: true }).click();
    await expectVisible(page.getByText('已刷新 3 个可用模型。', { exact: true }), '模型刷新反馈');
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

    return { screenshots, apiCalls: state.apiCalls };
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
    await page.goto(`${baseUrl}/app`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.locator('.workbench-shell'), '直接进入工作台');
    assert.equal(await page.locator('.ambient-backdrop').count(), 0, '工作台不应挂载认证背景');
    assert.deepEqual(state.mediaRequests, [], '直接进入工作台不应请求 MP4');
    await page.goto(`${baseUrl}/admin/users`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '用户', exact: true }), '直接进入管理后台');
    assert.equal(await page.locator('.ambient-backdrop').count(), 0, '管理后台不应挂载认证背景');
    assert.deepEqual(state.mediaRequests, [], '直接进入管理后台不应请求 MP4');
    assert.deepEqual(state.pageErrors, [], `直接进入工作台脚本错误：${state.pageErrors.join(' | ')}`);
    return 'product-and-admin-routes-no-backdrop';
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
    const screenshot = resolve(outputDir, '07-change-password-1024x768.png');
    await page.screenshot({ path: screenshot, animations: 'disabled' });
    assert.ok(statSync(screenshot).size > 1000, `截图为空：${screenshot}`);
    return { status: 'change-password-neutral-product-surface', screenshot };
  } finally {
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
  const backdropMaterial = await runMotionBackdropAcceptance(browser);
  const constrainedBackdrop = await runConstrainedBackdropAcceptance(browser);
  const productIsolation = await runDirectProductIsolationAcceptance(browser);
  const changePasswordSurface = await runChangePasswordSurfaceAcceptance(browser);
  console.log(JSON.stringify({
    ok: true,
    baseUrl,
    backdropMaterial,
    constrainedBackdrop,
    productIsolation,
    changePasswordSurface,
    screenshotCount: result.screenshots.length,
    screenshots: result.screenshots,
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
