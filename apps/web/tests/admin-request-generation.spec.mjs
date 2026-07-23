import { describe, expect, it } from 'vitest';

describe('admin request generation guard', () => {
  it('只允许慢 A / 快 B 中最新的 B 提交并结束自己的 loading', async () => {
    const { createAdminRequestGenerationGuard } = await import('../src/views/adminRequestGeneration.ts');
    const guard = createAdminRequestGenerationGuard();
    const committed = [];
    let loading = true;

    const slowA = guard.begin();
    const fastB = guard.begin();

    if (guard.isCurrent(fastB)) {
      committed.push('B');
      loading = false;
    }
    if (guard.isCurrent(slowA)) {
      committed.push('A');
      loading = false;
    }

    expect(committed).toEqual(['B']);
    expect(loading).toBe(false);
  });

  it('切换用户或关闭审计后使所有旧响应和旧 finally 失效', async () => {
    const { createAdminRequestGenerationGuard } = await import('../src/views/adminRequestGeneration.ts');
    const guard = createAdminRequestGenerationGuard();
    const pending = guard.begin();

    guard.invalidate();

    expect(guard.isCurrent(pending)).toBe(false);
    expect(guard.begin()).toBeGreaterThan(pending);
  });
});
