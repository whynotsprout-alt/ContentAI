import { createPinia } from 'pinia';
import { createApp } from 'vue';
import Root from '@/app/Root.vue';
import { router } from '@/app/router';
import '@/shared/styles/index.css';

const app = createApp(Root);
app.use(createPinia());
app.use(router);
app.mount('#app');
