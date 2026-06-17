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
  await page.getByText('AI 内容生成 Demo').waitFor();
  await page.waitForFunction(() => {
    const select = document.querySelector('select');
    return select && select.options.length > 0;
  });
  await page.getByLabel('账号').selectOption('gaobailie-shuo-caijing');

  const prompt = '请生成一篇 AI 内容初稿，要求有标题和封面建议。';
  await page.locator('textarea').fill(prompt);
  await page.getByRole('button', { name: /发送/ }).click();

  await page.getByText('初稿已生成').waitFor({ timeout: 20000 });
  await page.getByText('初稿包 Markdown').waitFor({ timeout: 10000 });
  await page.getByText('候选选题').waitFor();
  await page.screenshot({ path: resolve(outputDir, 'articleforgeai-workbench.png'), fullPage: true });

  const statusText = await page.locator('.steps').innerText();
  const artifactText = await page.locator('.artifact-list').innerText();
  const draftText = await page.locator('.draft').innerText();

  console.log(
    JSON.stringify(
      {
        ok: true,
        statusText,
        artifactText,
        hasDraft: draftText.includes('初稿待人工审核'),
        screenshot: resolve(outputDir, 'articleforgeai-workbench.png')
      },
      null,
      2
    )
  );
} finally {
  await browser.close();
}
