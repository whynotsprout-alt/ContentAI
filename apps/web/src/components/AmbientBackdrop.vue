<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import backgroundImage from '../assets/backgrounds/web-background-poster.jpg';

type NetworkInformation = EventTarget & {
  effectiveType?: string;
  saveData?: boolean;
};

const video = ref<HTMLVideoElement | null>(null);
const backgroundVideo = ref('');
const motionAllowed = ref(false);
const videoReady = ref(false);
const videoFailed = ref(false);
let reducedMotionQuery: MediaQueryList | null = null;
let connection: NetworkInformation | null = null;
let videoSourcePromise: Promise<string> | null = null;

function pauseVideo() {
  video.value?.pause();
}

async function resumeVideo() {
  if (!motionAllowed.value || videoFailed.value || document.hidden) return;
  if (!backgroundVideo.value) {
    videoSourcePromise ??= import('../assets/backgrounds/web-background.mp4')
      .then((module) => module.default)
      .catch(() => {
        videoFailed.value = true;
        return '';
      });
    const source = await videoSourcePromise;
    if (!source || !motionAllowed.value || document.hidden) return;
    backgroundVideo.value = source;
  }
  await nextTick();
  try {
    await video.value?.play();
  } catch {
    // The static poster stays visible if an autoplay policy blocks playback.
  }
}

function syncMotionPreference() {
  const effectiveType = connection?.effectiveType;
  const constrainedConnection = Boolean(
    connection?.saveData
    || effectiveType === '2g'
    || effectiveType === 'slow-2g'
  );
  motionAllowed.value = !reducedMotionQuery?.matches && !constrainedConnection;
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
  connection = (navigator as Navigator & { connection?: NetworkInformation }).connection ?? null;
  reducedMotionQuery.addEventListener('change', syncMotionPreference);
  connection?.addEventListener('change', syncMotionPreference);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  syncMotionPreference();
});

onBeforeUnmount(() => {
  reducedMotionQuery?.removeEventListener('change', syncMotionPreference);
  connection?.removeEventListener('change', syncMotionPreference);
  document.removeEventListener('visibilitychange', handleVisibilityChange);
  pauseVideo();
});
</script>

<template>
  <div
    class="ambient-backdrop"
    :data-video-state="videoFailed ? 'failed' : videoReady ? 'ready' : 'poster'"
    data-material="web-background"
    aria-hidden="true"
  >
    <div
      class="ambient-backdrop__poster"
      :style="{ backgroundImage: `url(${backgroundImage})` }"
    ></div>
    <video
      v-if="motionAllowed && backgroundVideo && !videoFailed"
      ref="video"
      class="ambient-backdrop__video"
      :class="{ 'is-ready': videoReady }"
      :src="backgroundVideo"
      :poster="backgroundImage"
      autoplay
      loop
      muted
      playsinline
      preload="none"
      @canplay="handleVideoReady"
      @error="handleVideoError"
    ></video>
  </div>
</template>
