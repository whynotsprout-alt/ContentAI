<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import backgroundVideo from '../assets/backgrounds/web-background.mp4';
import backgroundImage from '../assets/backgrounds/web-background-poster.jpg';

type AmbientBackdropMode = 'hero' | 'workspace' | 'admin';

const props = defineProps<{
  mode: AmbientBackdropMode;
}>();

const video = ref<HTMLVideoElement | null>(null);
const motionAllowed = ref(false);
const videoReady = ref(false);
const videoFailed = ref(false);
let reducedMotionQuery: MediaQueryList | null = null;

function pauseVideo() {
  video.value?.pause();
}

async function resumeVideo() {
  if (!motionAllowed.value || videoFailed.value || document.hidden) return;
  await nextTick();
  try {
    await video.value?.play();
  } catch {
    // The static poster stays visible if an autoplay policy blocks playback.
  }
}

function syncMotionPreference() {
  motionAllowed.value = !reducedMotionQuery?.matches;
  if (!motionAllowed.value) {
    videoReady.value = false;
    pauseVideo();
    return;
  }
  void resumeVideo();
}

function handleVisibilityChange() {
  if (document.hidden) {
    pauseVideo();
    return;
  }
  void resumeVideo();
}

function handleVideoReady() {
  videoReady.value = true;
  if (document.hidden) pauseVideo();
}

function handleVideoError() {
  videoFailed.value = true;
  videoReady.value = false;
}

onMounted(() => {
  reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  reducedMotionQuery.addEventListener('change', syncMotionPreference);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  syncMotionPreference();
});

onBeforeUnmount(() => {
  reducedMotionQuery?.removeEventListener('change', syncMotionPreference);
  document.removeEventListener('visibilitychange', handleVisibilityChange);
  pauseVideo();
});
</script>

<template>
  <div
    class="ambient-backdrop"
    :class="`ambient-backdrop--${mode}`"
    :data-video-state="videoFailed ? 'failed' : videoReady ? 'ready' : 'poster'"
    data-material="web-background"
    aria-hidden="true"
  >
    <div
      class="ambient-backdrop__poster"
      :style="{ backgroundImage: `url(${backgroundImage})` }"
    ></div>
    <video
      v-if="motionAllowed && !videoFailed"
      ref="video"
      class="ambient-backdrop__video"
      :class="{ 'is-ready': videoReady }"
      :src="backgroundVideo"
      :poster="backgroundImage"
      autoplay
      loop
      muted
      playsinline
      preload="metadata"
      @canplay="handleVideoReady"
      @error="handleVideoError"
    ></video>
    <div class="ambient-backdrop__tint"></div>
  </div>
</template>
