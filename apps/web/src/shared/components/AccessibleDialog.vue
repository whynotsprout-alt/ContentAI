<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

const props = withDefaults(defineProps<{
  titleId: string;
  variant?: 'default' | 'fullscreen';
  busy?: boolean;
  closeOnBackdrop?: boolean;
  overlayClass?: string;
}>(), {
  variant: 'default',
  busy: false,
  closeOnBackdrop: true,
  overlayClass: ''
});

const emit = defineEmits<{ close: [] }>();
const overlay = ref<HTMLElement | null>(null);
const dialog = ref<HTMLElement | null>(null);
const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
const backgroundState: Array<{ element: HTMLElement; inert: boolean; ariaHidden: string | null }> = [];

function focusableElements() {
  if (!dialog.value) return [];
  return Array.from(dialog.value.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true');
}

function requestClose() {
  if (!props.busy) emit('close');
}

function onBackdrop(event: MouseEvent) {
  if (props.closeOnBackdrop && event.target === overlay.value) requestClose();
}

function onKeydown(event: KeyboardEvent) {
  const overlays = Array.from(document.querySelectorAll<HTMLElement>('.modal-backdrop'));
  if (overlays[overlays.length - 1] !== overlay.value) return;
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
  if (document.querySelectorAll('.modal-backdrop').length <= 1) {
    document.body.classList.remove('dialog-open');
  }
  for (const state of backgroundState) {
    state.element.inert = state.inert;
    if (state.ariaHidden === null) state.element.removeAttribute('aria-hidden');
    else state.element.setAttribute('aria-hidden', state.ariaHidden);
  }
  void nextTick(() => previousFocus?.focus());
});
</script>

<template>
  <Teleport to="body">
    <div ref="overlay" class="modal-backdrop" :class="[`modal-${variant}`, overlayClass]" @mousedown="onBackdrop">
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
