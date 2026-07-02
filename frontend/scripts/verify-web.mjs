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
  await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' });

  await page.getByText('ContentAI').waitFor();
  await page.getByText('AI 鍐呭鐢熸垚 Demo').waitFor();
  const accountTrigger = page.locator('.account-trigger');
  await accountTrigger.click();
  await page.locator('.account-menu .account-option').getByText('gaobailie-shuo-caijing').click();

  const prompt = '璇风敓鎴愪竴绡?AI 鍐呭鍒濈锛岃姹傛湁鏍囬鍜屽皝闈㈠缓璁€?;
  await page.locator('textarea').fill(prompt);
  await page.getByRole('button', { name: /鍙戦€? }).click();

  await page.getByText('鍒濈宸茬敓鎴?).waitFor({ timeout: 20000 });
  await page.getByText('鍒濈鍖?Markdown').waitFor({ timeout: 10000 });
  await page.getByText('鍊欓€夐€夐').waitFor();
  await page.screenshot({ path: resolve(outputDir, 'contentai-workbench.png'), fullPage: true });

  const statusText = await page.locator('.sidebar-steps').innerText();
  const draftText = await page.locator('.draft-list').innerText();

  console.log(
    JSON.stringify(
        {
        ok: true,
        statusText,
        hasDraft: draftText.includes('内容草稿'),
        screenshot: resolve(outputDir, 'contentai-workbench.png')
        },
      null,
      2
    )
  );
} finally {
  await browser.close();
}
