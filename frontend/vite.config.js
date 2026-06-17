var _a;
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
var apiTarget = (_a = process.env.VITE_API_TARGET) !== null && _a !== void 0 ? _a : 'http://127.0.0.1:8001';
export default defineConfig({
    plugins: [vue()],
    server: {
        port: 5173,
        host: '127.0.0.1',
        proxy: {
            '/api': {
                target: apiTarget,
                changeOrigin: true
            }
        }
    }
});
