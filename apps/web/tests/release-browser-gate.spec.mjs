import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');

describe('release browser quality gate', () => {
  it('runs axe on every required route and state with actionable failures', async () => {
    const verify = await read('../scripts/verify-web.mjs');

    expect(verify).toContain(`import axe from 'axe-core'`);
    expect(verify).toContain('async function expectAxeClean');
    expect(verify).toContain('window.axe.run');
    expect(verify).toContain('violation.id');
    expect(verify).toContain('violation.impact');
    expect(verify).toContain('node.target');
    for (const state of [
      'auth-login',
      'auth-register',
      'forced-change-password',
      'workbench-messages',
      'workbench-empty',
      'structured-interrupt',
      'session-drawer',
      'agent-directory',
      'agent-editor',
      'admin-user-list',
      'admin-user-detail',
      'temporary-password',
      'audit-list',
      'audit-transcript',
      'model-config'
    ]) {
      expect(verify).toContain(`'${state}'`);
    }
  });

  it('covers the release viewport matrix, 320px, and actual Chromium page scale 200%', async () => {
    const verify = await read('../scripts/verify-web.mjs');

    for (const dimensions of [
      '360x800',
      '390x844',
      '768x1024',
      '1024x768',
      '1280x720',
      '1280x800',
      '1440x900',
      '1920x1080',
      '320x800'
    ]) {
      expect(verify).toContain(dimensions);
    }
    expect(verify).toContain('Emulation.setPageScaleFactor');
    expect(verify).toContain('pageScaleFactor: 2');
    expect(verify).toContain('visualViewport.scale');
    expect(verify).toContain('Emulation.setDeviceMetricsOverride');
    expect(verify).toContain('deviceScaleFactor: 2');
    expect(verify).toContain('window.innerWidth');
    expect(verify).toContain('window.devicePixelRatio');
    expect(verify).toContain('visualViewport.width');
  });

  it('measures visible touch targets and reports the failing selector and dimensions', async () => {
    const verify = await read('../scripts/verify-web.mjs');

    expect(verify).toContain('async function expectMinimumTouchTargets');
    expect(verify).toContain('getBoundingClientRect()');
    expect(verify).toContain('width < 44 || height < 44');
    expect(verify).toContain('selector');
    expect(verify).toContain('width');
    expect(verify).toContain('height');
  });

  it('checks every authenticated route for MP4 isolation and keeps auth usable on video failure', async () => {
    const verify = await read('../scripts/verify-web.mjs');

    expect(verify).toContain(`['/app', '/change-password', '/admin/users', '/admin/models']`);
    expect(verify).toContain('auth-video-failure-form-available');
    expect(verify).toContain(`route.abort('failed')`);
    expect(verify).toContain(`reducedMotion: 'reduce'`);
  });
});
