import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const appSource = readFileSync(resolve(process.cwd(), 'src/App.vue'), 'utf8');
const browserConfirm = ['window', 'confirm'].join('.');

describe('destructive delete confirmation', () => {
  it('uses the in-app confirmation modal instead of browser confirm', () => {
    expect(appSource).not.toContain(browserConfirm);
    expect(appSource).toContain('deleteTarget');
    expect(appSource).toContain('AccessibleDialog');
    expect(appSource).toContain('确认删除');
  });
});
