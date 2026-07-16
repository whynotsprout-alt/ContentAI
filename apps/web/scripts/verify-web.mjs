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
      return fulfillJson(route, [workbenchSessionSummary, secondSessionSummary]);
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
        latest_execution: null
      });
    }
    if (path === '/api/admin/users' && method === 'GET') {
      return fulfillJson(route, { items: [targetUser], page: 1, page_size: 20, total: 1 });
    }
    if (path === `/api/admin/users/${targetUser.id}` && method === 'GET') {
      return fulfillJson(route, targetUser);
    }
    if (path === `/api/admin/users/${targetUser.id}/sessions` && method === 'GET') {
      return fulfillJson(route, { items: [adminSessionSummary], page: 1, page_size: 30, total: 1 });
    }
    if (path === '/api/admin/usage' && method === 'GET') {
      return fulfillJson(route, { items: usageBuckets });
    }
    if (path === `/api/admin/sessions/${adminSessionSummary.session_id}` && method === 'GET') {
      return fulfillJson(route, adminSessionDetail);
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
    failedRequests: []
  };
  const screenshots = [];
  page.on('pageerror', (error) => state.pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') state.consoleErrors.push(message.text());
  });
  page.on('response', (response) => {
    const expectedAnonymousMe = !state.authenticated && response.url().includes('/api/auth/me') && response.status() === 401;
    if (response.status() >= 400 && !expectedAnonymousMe) {
      state.failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on('requestfailed', (request) => {
    state.failedRequests.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText ?? 'unknown'}`);
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

    await page.getByRole('textbox', { name: '邮箱', exact: true }).fill(authUser.email);
    await page.locator('input[type="password"]').first().fill('verify-web-password');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.waitForURL('**/app');

    await expectVisible(page.locator('.workbench-shell'), '工作台');
    await expectVisible(page.getByText(agent.name, { exact: true }).first(), '当前内容账号');
    await expectVisible(page.getByText('平台补贴与消费趋势', { exact: true }), '会话条目');
    await expectVisible(page.getByText('你以为平台又在撒钱，其实它们真正争夺的，是你下一次消费时第一个打开谁。', { exact: true }), '对话消息');
    await capture(page, '02-workbench-1440x900.png', screenshots);

    for (const viewport of [
      { width: 1280, height: 720, file: '02b-workbench-1280x720.png' },
      { width: 1920, height: 1080, file: '02c-workbench-1920x1080.png' },
      { width: 2560, height: 1440, file: '02d-workbench-2560x1440.png' }
    ]) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await expectHidden(page.locator('.desktop-gate'), `${viewport.width}px 桌面门槛`);
      await capture(page, viewport.file, screenshots);
    }
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.getByRole('button', { name: '内容账号', exact: true }).click();
    await expectVisible(page.getByRole('heading', { name: '配置你的 ContentAI', exact: true }), '内容账号配置');
    await expectVisible(page.getByRole('textbox', { name: '账号名称', exact: true }), '账号名称字段');
    await page.getByRole('textbox', { name: '账号名称', exact: true }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: '账号名称', exact: true }).inputValue(), agent.name);
    const managerColumns = await page.locator('.manager-layout').evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    assert.match(managerColumns, /^280px 184px /, `账号配置列宽不正确：${managerColumns}`);
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
    await capture(page, '05-admin-audit-1440x900.png', screenshots);

    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto(`${baseUrl}/app`, { waitUntil: 'domcontentloaded' });
    await expectVisible(page.getByRole('heading', { name: '请在桌面浏览器中打开', exact: true }), '1024px 桌面门槛');
    await expectHidden(page.locator('.desktop-application'), '1024px 应用界面');
    await capture(page, '06-desktop-gate-1024x768.png', screenshots);

    assert.deepEqual(state.unexpectedApiCalls, [], `存在未模拟 API：${state.unexpectedApiCalls.join(', ')}`);
    assert.deepEqual(state.pageErrors, [], `页面脚本错误：${state.pageErrors.join(' | ')}`);
    assert.deepEqual(state.failedResponses, [], `存在失败响应：${state.failedResponses.join(' | ')}`);
    assert.deepEqual(state.failedRequests, [], `存在失败请求：${state.failedRequests.join(' | ')}`);
    assert.ok(state.apiCalls.some((call) => call === 'POST /api/auth/login'), '未覆盖登录 API');
    assert.ok(state.apiCalls.some((call) => call.startsWith(`GET /api/admin/users/${targetUser.id}/sessions`)), '未覆盖后台会话列表 API');
    assert.ok(state.apiCalls.some((call) => call === `GET /api/admin/sessions/${adminSessionSummary.session_id}`), '未覆盖会话审计 API');

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
    assert.equal(await page.locator('.ambient-backdrop__poster').isVisible(), true, '动态背景的静态后备应保持可见');
    assert.equal(await page.locator('.ambient-backdrop').getAttribute('data-material'), 'web-background');
    assert.equal(await page.locator('.ambient-backdrop__video').count(), 1, '未减少动画模式应加载本地视频节点');
    return 'web-background-local';
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
  console.log(JSON.stringify({
    ok: true,
    baseUrl,
    backdropMaterial,
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
