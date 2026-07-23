<script lang="ts">
const dialogStack: HTMLElement[] = [];
</script>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

const focusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  'details > summary:first-of-type',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])'
].join(', ');

const props = withDefaults(defineProps<{
  titleId: string;
  variant?: 'default' | 'fullscreen' | 'drawer';
  surface?: 'product';
  busy?: boolean;
  closeOnBackdrop?: boolean;
}>(), {
  variant: 'default',
  surface: 'product',
  busy: false,
  closeOnBackdrop: true
});

const emit = defineEmits<{ close: [] }>();
const overlay = ref<HTMLElement | null>(null);
const dialog = ref<HTMLElement | null>(null);
const layer = ref(0);
const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
const backgroundState: Array<{ element: HTMLElement; inert: boolean; ariaHidden: string | null }> = [];

function isVisible(element: HTMLElement) {
  if (
    element.hidden
    || element.closest('[hidden], [inert], [aria-hidden="true"]')
    || element.getClientRects().length === 0
  ) return false;
  const style = getComputedStyle(element);
  return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
}

function focusableElements() {
  if (!dialog.value) return [];
  return Array.from(dialog.value.querySelectorAll<HTMLElement>(focusableSelector)).filter(isVisible);
}

function isTopDialog() {
  return dialogStack[dialogStack.length - 1] === overlay.value;
}

function requestClose() {
  if (!props.busy && (!overlay.value || isTopDialog())) emit('close');
}

function onBackdrop(event: MouseEvent) {
  if (isTopDialog() && props.closeOnBackdrop && event.target === overlay.value) requestClose();
}

function onKeydown(event: KeyboardEvent) {
  if (!isTopDialog()) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    requestClose();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = focusableElements();
  if (!focusable.length) {
    event.preventDefault();
    dialog.value?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

onMounted(() => {
  if (!overlay.value) return;
  dialogStack.push(overlay.value);
  layer.value = dialogStack.length;
  document.body.classList.add('dialog-open');
  for (const child of Array.from(document.body.children)) {
    if (!(child instanceof HTMLElement) || child === overlay.value || child.contains(overlay.value)) continue;
    backgroundState.push({ element: child, inert: child.inert, ariaHidden: child.getAttribute('aria-hidden') });
    child.inert = true;
    child.setAttribute('aria-hidden', 'true');
  }
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => (focusableElements()[0] ?? dialog.value)?.focus());
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  if (overlay.value) {
    const index = dialogStack.indexOf(overlay.value);
    if (index >= 0) dialogStack.splice(index, 1);
    dialogStack.forEach((element, stackIndex) => {
      element.dataset.dialogLayer = String(stackIndex + 1);
    });
  }
  for (const state of backgroundState) {
    state.element.inert = state.inert;
    if (state.ariaHidden === null) state.element.removeAttribute('aria-hidden');
    else state.element.setAttribute('aria-hidden', state.ariaHidden);
  }
  if (!dialogStack.length) document.body.classList.remove('dialog-open');
  void nextTick(() => {
    if (previousFocus?.isConnected && isVisible(previousFocus)) previousFocus?.focus();
  });
});
</script>

<template>
  <Teleport to="body">
    <div
      ref="overlay"
      class="modal-backdrop"
      :class="`modal-${variant}`"
      :data-dialog-layer="layer"
      :data-surface="surface"
      @mousedown="onBackdrop"
    >
      <section
        ref="dialog"
        class="accessible-dialog"
        :class="`accessible-dialog-${variant}`"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        :aria-busy="busy || undefined"
        tabindex="-1"
      >
        <slot :request-close="requestClose" />
      </section>
    </div>
  </Teleport>
</template>
