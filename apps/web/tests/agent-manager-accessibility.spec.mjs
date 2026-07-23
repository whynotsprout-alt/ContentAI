import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

describe('agent manager accessibility and adaptation contracts', () => {
  it('supports drawer presentation and a local product theme on teleported dialogs', async () => {
    const [dialog, dialogsCss, productCss] = await Promise.all([
      read('../src/components/AccessibleDialog.vue'),
      read('../src/styles/dialogs.css'),
      read('../src/styles/product.css')
    ]);

    expect(dialog).toContain("'drawer'");
    expect(dialog).toContain(':data-surface="surface"');
    expect(dialog).toContain("'--dialog-layer': layer");
    expect(dialogsCss).toContain('.accessible-dialog-drawer');
    expect(dialogsCss).toContain('.modal-drawer');
    expect(productCss).toContain(".modal-backdrop[data-surface='product']");
    expect(productCss).toMatch(
      /\[data-surface='product'\] \.accessible-dialog-drawer[\s\S]*border-radius:\s*var\(--product-radius-overlay\) 0 0 var\(--product-radius-overlay\)/
    );
  });

  it('tracks nested layers, limits dismissal to the top layer, and filters invisible focus targets', async () => {
    const [dialog, stack] = await Promise.all([
      read('../src/components/AccessibleDialog.vue'),
      read('../src/components/dialogStack.ts')
    ]);

    expect(dialog).toContain('dialogStack');
    expect(dialog).toContain('data-dialog-layer');
    expect(dialog).toContain('isTopDialog');
    expect(dialog).toContain('getClientRects().length');
    expect(stack).toContain('surface.inert = true');
    expect(stack).toContain('firstTrigger');
    expect(dialog).toContain("event.key === 'Escape'");
    expect(dialog).toContain('previousFocus');
  });

  it('implements a directory-to-editor compact flow and standard roving tabs', async () => {
    const manager = await read('../src/components/AgentManager.vue');

    expect(manager).toContain("ref<'directory' | 'editor'>('directory')");
    expect(manager).toContain('focusDirectoryTarget');
    expect(manager).toContain(':data-manager-view="compactView"');
    expect(manager).toContain('class="manager-back-button"');
    expect(manager).toContain('role="tablist"');
    expect(manager).toContain(":aria-orientation=\"compactLayout ? 'horizontal' : 'vertical'\"");
    expect(manager).toContain('role="tab"');
    expect(manager).toContain(':aria-selected="activeSection === section.id"');
    expect(manager).toContain(':tabindex="activeSection === section.id ? 0 : -1"');
    expect(manager).toContain('@keydown="onSectionKeydown($event, section.id)"');
    expect(manager).toContain("case 'ArrowRight'");
    expect(manager).toContain("case 'ArrowDown'");
    expect(manager).toContain("case 'ArrowUp'");
    expect(manager).toContain("case 'Home'");
    expect(manager).toContain("case 'End'");
    expect(manager).toContain(':hidden="activeSection !==');
  });

  it('keeps a single compact editor scroller, safe-area actions, and the desktop three-column layout', async () => {
    const dialogsCss = await read('../src/styles/dialogs.css');

    expect(dialogsCss).toMatch(
      /@media \(min-width:\s*1280px\)[\s\S]*grid-template-columns:\s*280px 184px minmax\(0,\s*1fr\)/
    );
    expect(dialogsCss).toMatch(/\.manager-layout\[data-manager-view='editor'\][\s\S]*grid-template-rows:\s*auto minmax\(0,\s*1fr\)/);
    expect(dialogsCss).toMatch(/\.editor-footer[\s\S]*env\(safe-area-inset-bottom\)/);
    expect(dialogsCss).toMatch(/\.editor-section[\s\S]*overflow-y:\s*auto/);
  });

  it('keeps real browser acceptance for compact views, tabs, nested dialogs, focus, and scroll containment', async () => {
    const verify = await read('../scripts/verify-web.mjs');

    expect(verify).toContain('03a-agent-manager-360x800-directory.png');
    expect(verify).toContain('03b-agent-manager-360x800-editor.png');
    expect(verify).toContain('03c-agent-manager-768x1024-editor.png');
    expect(verify).toContain('03d-agent-manager-1280x800.png');
    expect(verify).toContain('data-dialog-layer');
    expect(verify).toContain('aria-selected');
    expect(verify).toContain('ArrowRight');
    expect(verify).toContain('Home');
    expect(verify).toContain('End');
    expect(verify).toContain('vertical scroll container');
    expect(verify).toContain('根页面应在最外层弹窗关闭后恢复');
  });
});
