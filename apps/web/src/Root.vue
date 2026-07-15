<script setup lang="ts">
import { computed } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import AmbientBackdrop from './components/AmbientBackdrop.vue';

type AmbientBackdropMode = 'hero' | 'workspace' | 'admin';

const route = useRoute();

const visualMode = computed<AmbientBackdropMode>(() => {
  const mode = route.meta.visualMode;
  return mode === 'hero' || mode === 'admin' ? mode : 'workspace';
});
</script>

<template>
  <div class="root-shell" :data-visual-mode="visualMode">
    <AmbientBackdrop :mode="visualMode" />

    <section class="desktop-gate" aria-labelledby="desktop-gate-title">
      <div class="desktop-gate__mark" aria-hidden="true">C</div>
      <p class="desktop-gate__brand">ContentAI</p>
      <h1 id="desktop-gate-title">请在桌面浏览器中打开</h1>
      <p>当前工作台针对宽度 1280px 及以上的桌面屏幕设计。</p>
    </section>

    <div class="desktop-application">
      <RouterView />
    </div>
  </div>
</template>
