export interface DialogSurface {
  inert: boolean;
  getAttribute(name: string): string | null;
  setAttribute(name: string, value: string): void;
  removeAttribute(name: string): void;
  focus(): void;
}

type SurfaceState = {
  inert: boolean;
  ariaHidden: string | null;
};

type DialogEntry<T extends DialogSurface> = {
  overlay: T;
  returnFocus: T | null;
  setLayer: (layer: number) => void;
};

export type DialogFocusPlan<T extends DialogSurface> = {
  generation: number;
  target: T | null;
} | null;

export function createDialogStackCoordinator<T extends DialogSurface>(options: {
  surfaces: () => T[];
  canFocus: (surface: T) => boolean;
}) {
  const entries: Array<DialogEntry<T>> = [];
  const baseline = new Map<T, SurfaceState>();
  let firstTrigger: T | null = null;
  let focusGeneration = 0;

  function captureNewSurfaces() {
    for (const surface of options.surfaces()) {
      if (baseline.has(surface)) continue;
      baseline.set(surface, {
        inert: surface.inert,
        ariaHidden: surface.getAttribute('aria-hidden')
      });
    }
  }

  function restore(surface: T, state: SurfaceState) {
    surface.inert = state.inert;
    if (state.ariaHidden === null) surface.removeAttribute('aria-hidden');
    else surface.setAttribute('aria-hidden', state.ariaHidden);
  }

  function recompute() {
    captureNewSurfaces();
    const top = entries[entries.length - 1]?.overlay ?? null;
    for (const surface of options.surfaces()) {
      const state = baseline.get(surface);
      if (!state) continue;
      if (surface === top) {
        restore(surface, state);
      } else {
        surface.inert = true;
        surface.setAttribute('aria-hidden', 'true');
      }
    }
    entries.forEach((entry, index) => entry.setLayer(index + 1));
  }

  function register(overlay: T, returnFocus: T | null, setLayer: (layer: number) => void) {
    if (!entries.length) {
      baseline.clear();
      firstTrigger = returnFocus;
    }
    focusGeneration += 1;
    captureNewSurfaces();
    entries.push({ overlay, returnFocus, setLayer });
    recompute();
  }

  function unregister(overlay: T): DialogFocusPlan<T> {
    const wasTop = entries[entries.length - 1]?.overlay === overlay;
    const index = entries.findIndex((entry) => entry.overlay === overlay);
    if (index < 0) return null;
    const [removed] = entries.splice(index, 1);
    removed.setLayer(0);
    focusGeneration += 1;

    if (entries.length) {
      recompute();
      return {
        generation: focusGeneration,
        target: wasTop ? removed.returnFocus : null
      };
    }

    captureNewSurfaces();
    for (const [surface, state] of baseline) restore(surface, state);
    baseline.clear();
    const target = firstTrigger;
    firstTrigger = null;
    return { generation: focusGeneration, target };
  }

  function restoreFocus(plan: DialogFocusPlan<T>) {
    if (!plan || plan.generation !== focusGeneration || !plan.target || !options.canFocus(plan.target)) return;
    plan.target.focus();
  }

  return {
    register,
    unregister,
    restoreFocus,
    isTop: (overlay: T | null) => Boolean(overlay && entries[entries.length - 1]?.overlay === overlay),
    get size() {
      return entries.length;
    }
  };
}

function isVisibleFocusTarget(element: HTMLElement) {
  if (
    !element.isConnected
    || element.hidden
    || element.closest('[hidden], [inert], [aria-hidden="true"]')
    || element.getClientRects().length === 0
  ) return false;
  const style = getComputedStyle(element);
  return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
}

export const dialogStack = createDialogStackCoordinator<HTMLElement>({
  surfaces: () => Array.from(document.body.children).filter((element): element is HTMLElement => element instanceof HTMLElement),
  canFocus: isVisibleFocusTarget
});
