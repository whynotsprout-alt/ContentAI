import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';

const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const outputDir = resolve(process.cwd(), '..', '..', 'output', 'playwright');
mkdirSync(outputDir, { recursive: true });

const browser = await chromium.launch({
  executablePath: chromePath,
  headless: true
});

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  await page.goto('http://127.0.0.1:5180', { waitUntil: 'networkidle' });

  await page.getByText('ContentAI').waitFor();
  await page.getByText('持续对话工作台').waitFor();
  await page.getByText('会话记录').waitFor();
  await page.getByText('工作台').waitFor();

  const accountTrigger = page.locator('.account-trigger');
  const selectedAccount = (await accountTrigger.innerText()).trim();
  const hasAccount = selectedAccount !== '请选择账号';

  if (hasAccount) {
    const prompt = '请生成一段 ContentAI 工作台验收用的简短回复。';
    await page.locator('textarea').fill(prompt);
    await page.getByRole('button', { name: /发送/ }).click();
    await page.locator('.message.user').getByText(prompt).waitFor();
  }

  await page.screenshot({ path: resolve(outputDir, 'contentai-workbench.png'), fullPage: true });

  const sessionText = await page.locator('.session-list').innerText();
  const chatText = await page.locator('.chat-panel').innerText();

  console.log(
    JSON.stringify(
        {
        ok: true,
        hasAccount,
        sessionText,
        chatText,
        screenshot: resolve(outputDir, 'contentai-workbench.png')
        },
      null,
      2
    )
  );
} finally {
  await browser.close();
}
