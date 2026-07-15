import { createPinia } from 'pinia';
import { createApp } from 'vue';
import Root from './Root.vue';
import { router } from './router';
import './styles.css';

const app = createApp(Root);
app.use(createPinia());
app.use(router);
app.mount('#app');
