import { describe, expect, it } from 'vitest';

const loadCoordinator = () => import('../src/components/dialogStack.ts').catch(() => null);

class FakeSurface {
  constructor(name, { inert = false, ariaHidden = null, focusable = true } = {}) {
    this.name = name;
    this.inert = inert;
    this.ariaHidden = ariaHidden;
    this.focusable = focusable;
    this.focusCount = 0;
  }

  getAttribute(name) {
    return name === 'aria-hidden' ? this.ariaHidden : null;
  }

  setAttribute(name, value) {
    if (name === 'aria-hidden') this.ariaHidden = value;
  }

  removeAttribute(name) {
    if (name === 'aria-hidden') this.ariaHidden = null;
  }

  focus() {
    this.focusCount += 1;
  }
}

function createFixture(createDialogStackCoordinator, rootOptions) {
  const root = new FakeSurface('root', rootOptions);
  const outer = new FakeSurface('outer');
  const inner = new FakeSurface('inner');
  const trigger = new FakeSurface('trigger');
  const innerTrigger = new FakeSurface('inner-trigger');
  const surfaces = [root, outer];
  const layers = new Map();
  const coordinator = createDialogStackCoordinator({
    surfaces: () => surfaces,
    canFocus: (surface) => surface.focusable
  });
  return { root, outer, inner, trigger, innerTrigger, surfaces, layers, coordinator };
}

describe('dialog stack runtime coordinator', () => {
  it('is an executable module instead of a source-string contract', async () => {
    const module = await loadCoordinator();
    expect(module).not.toBeNull();
    expect(module?.createDialogStackCoordinator).toBeTypeOf('function');
  });

  it('recomputes inert state and restores the first trigger when inner unmounts first', async () => {
    const module = await loadCoordinator();
    expect(module).not.toBeNull();
    if (!module) return;
    const fixture = createFixture(module.createDialogStackCoordinator);
    const { coordinator, root, outer, inner, trigger, innerTrigger, surfaces, layers } = fixture;

    coordinator.register(outer, trigger, (value) => layers.set(outer, value));
    surfaces.push(inner);
    coordinator.register(inner, innerTrigger, (value) => layers.set(inner, value));

    expect(root.inert).toBe(true);
    expect(outer.inert).toBe(true);
    expect(inner.inert).toBe(false);
    expect([...layers.values()]).toEqual([1, 2]);

    const innerPlan = coordinator.unregister(inner);
    expect(root.inert).toBe(true);
    expect(outer.inert).toBe(false);
    const outerPlan = coordinator.unregister(outer);
    coordinator.restoreFocus(innerPlan);
    coordinator.restoreFocus(outerPlan);

    expect(root.inert).toBe(false);
    expect(root.ariaHidden).toBeNull();
    expect(trigger.focusCount).toBe(1);
    expect(innerTrigger.focusCount).toBe(0);
    expect(coordinator.size).toBe(0);
  });

  it('restores the same baseline and first trigger when outer unmounts first', async () => {
    const module = await loadCoordinator();
    expect(module).not.toBeNull();
    if (!module) return;
    const fixture = createFixture(module.createDialogStackCoordinator);
    const { coordinator, root, outer, inner, trigger, innerTrigger, surfaces } = fixture;

    coordinator.register(outer, trigger, () => {});
    surfaces.push(inner);
    coordinator.register(inner, innerTrigger, () => {});

    const outerPlan = coordinator.unregister(outer);
    expect(root.inert).toBe(true);
    expect(inner.inert).toBe(false);
    const innerPlan = coordinator.unregister(inner);
    coordinator.restoreFocus(outerPlan);
    coordinator.restoreFocus(innerPlan);

    expect(root.inert).toBe(false);
    expect(root.ariaHidden).toBeNull();
    expect(trigger.focusCount).toBe(1);
    expect(innerTrigger.focusCount).toBe(0);
    expect(coordinator.size).toBe(0);
  });

  it('restores pre-existing inert and aria-hidden values exactly', async () => {
    const module = await loadCoordinator();
    expect(module).not.toBeNull();
    if (!module) return;
    const { coordinator, root, outer, trigger } = createFixture(
      module.createDialogStackCoordinator,
      { inert: true, ariaHidden: 'legacy' }
    );

    coordinator.register(outer, trigger, () => {});
    const plan = coordinator.unregister(outer);
    coordinator.restoreFocus(plan);

    expect(root.inert).toBe(true);
    expect(root.ariaHidden).toBe('legacy');
    expect(trigger.focusCount).toBe(1);
  });
});
