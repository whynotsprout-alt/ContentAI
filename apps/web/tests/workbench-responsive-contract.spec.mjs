import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');
const readOptional = async (path) => {
  try {
    return await read(path);
  } catch (error) {
    if (error && typeof error === 'object' && error.code === 'ENOENT') return '';
    throw error;
  }
};

describe('quiet product workbench contract', () => {
  it('keeps the auth backdrop lazy and removes the desktop gate from the root shell', async () => {
    const [root, auth, backdrop] = await Promise.all([
      read('../src/Root.vue'),
      read('../src/views/AuthView.vue'),
      read('../src/components/AmbientBackdrop.vue')
    ]);

    expect(root).not.toContain('AmbientBackdrop');
    expect(root).not.toContain('desktop-gate');
    expect(root).not.toMatch(/web-background\.(?:mp4|jpg)/);
    expect(auth).toContain("defineAsyncComponent(() => import('../components/AmbientBackdrop.vue'))");
    expect(auth).toContain('<AuthBackdrop mode="hero" />');
    expect(backdrop).toContain('saveData');
    expect(backdrop).toContain("effectiveType === '2g'");
    expect(backdrop).toContain("effectiveType === 'slow-2g'");
  });

  it('defines the restrained product palette, spacing, radii, and reading measure exactly', async () => {
    const productCss = await readOptional('../src/styles/product.css');

    expect(productCss).toContain('--product-bg: #F5F5F2');
    expect(productCss).toContain('--product-surface: #FFFFFF');
    expect(productCss).toContain('--product-surface-subtle: #ECEFEA');
    expect(productCss).toContain('--product-text: #171A18');
    expect(productCss).toContain('--product-muted: #5F6761');
    expect(productCss).toContain('--product-border: #D8DDD8');
    expect(productCss).toContain('--product-accent: #1F5B45');
    expect(productCss).toContain('--product-accent-hover: #174936');
    expect(productCss).toContain('--product-danger: #B42318');
    expect(productCss).toContain('--product-warning: #8A5A00');
    expect(productCss).toContain('--product-success: #247A52');
    expect(productCss).toContain('--product-focus: #2E7D60');
    expect(productCss).toMatch(/--product-space-(?:1|2|3|4|6|8|12):\s*(?:4|8|12|16|24|32|48)px/g);
    expect(productCss).toContain('--product-radius-control: 8px');
    expect(productCss).toContain('--product-radius-panel: 12px');
    expect(productCss).toContain('--product-radius-overlay: 16px');
    expect(productCss).toContain('--conversation-measure: 960px');
    expect(productCss).not.toMatch(/backdrop-filter|linear-gradient|radial-gradient|repeating-linear-gradient/);
  });

  it('implements one A1 session rail across mobile, tablet, and desktop breakpoints', async () => {
    const [app, header, productCss] = await Promise.all([
      read('../src/App.vue'),
      read('../src/components/WorkbenchHeader.vue'),
      readOptional('../src/styles/product.css')
    ]);

    expect(app.match(/<SessionRail\b/g)).toHaveLength(1);
    expect(app).toContain('id="session-navigation"');
    expect(app).toContain('@close="closeSessionNavigation"');
    expect(app).toContain('SESSION_RAIL_EXPANDED_KEY');
    expect(header).toContain('aria-controls="session-navigation"');
    expect(header).toContain(':aria-expanded="sessionNavigationExpanded"');
    expect(productCss).toMatch(/@media \(max-width: 767px\)/);
    expect(productCss).toContain('max-width: 360px');
    expect(productCss).toMatch(/@media \(min-width: 768px\) and \(max-width: 1023px\)/);
    expect(productCss).toContain('--session-rail-width: 72px');
    expect(productCss).toContain('--session-rail-width: 280px');
    expect(productCss).toMatch(/@media \(min-width: 1024px\) and \(max-width: 1279px\)[\s\S]*--session-rail-width:\s*248px/);
    expect(productCss).toMatch(/@media \(min-width: 1280px\) and \(max-width: 1439px\)[\s\S]*--session-rail-width:\s*264px/);
    expect(productCss).toMatch(/@media \(min-width: 1440px\)[\s\S]*--session-rail-width:\s*280px/);
  });

  it('provides complete modal-drawer dismissal and focus restoration behavior', async () => {
    const app = await read('../src/App.vue');

    expect(app).toContain("event.key === 'Escape'");
    expect(app).toContain("event.key !== 'Tab'");
    expect(app).toContain('class="session-rail-scrim"');
    expect(app).toContain('@click="closeSessionNavigation"');
    expect(app).toContain('sessionNavigationReturnFocus');
    expect(app).toContain('target.focus()');
    expect(app).toMatch(/async function loadSession[\s\S]*closeSessionNavigation/);
  });

  it('uses responsive Enter behavior and suppresses sends during IME composition', async () => {
    const chat = await read('../src/components/ChatCanvas.vue');

    expect(chat).toContain('event.isComposing');
    expect(chat).toContain('compositionActive.value');
    expect(chat).toContain('window.matchMedia');
    expect(chat).toContain("'(max-width: 767px)'");
    expect(chat).toContain('@compositionstart="compositionActive = true"');
    expect(chat).toContain('@compositionend="compositionActive = false"');
    expect(chat).not.toContain('@keydown.enter.prevent.exact="send"');
  });

  it('renders every structured interrupt field with batch approve, reject, and cancel controls', async () => {
    const [chat, approval] = await Promise.all([
      read('../src/components/ChatCanvas.vue'),
      readOptional('../src/components/InterruptApproval.vue')
    ]);

    expect(chat).toContain('<InterruptApproval');
    expect(approval).toContain('pendingInterrupt.actions');
    expect(approval).toContain('action.tool_name');
    expect(approval).toContain('action.purpose');
    expect(approval).toContain('action.memory.type');
    expect(approval).toContain('action.memory.content');
    expect(approval).toContain("emit('decide', 'approve')");
    expect(approval).toContain("emit('decide', 'reject')");
    expect(approval).toContain("emit('cancel')");
  });

  it('enables safe-area layout and stops loading the retired workbench stylesheet', async () => {
    const [index, styles] = await Promise.all([
      read('../index.html'),
      read('../src/styles.css')
    ]);

    expect(index).toContain('viewport-fit=cover');
    expect(styles).toContain("@import './styles/product.css';");
    expect(styles).not.toContain("@import './styles/workbench.css';");
  });
});
