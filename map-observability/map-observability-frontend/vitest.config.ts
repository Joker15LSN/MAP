import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // @agentscope-ai/design 未声明 exports/main,仅 module 字段,
      // 直接指向其 ESM 入口供 vitest 解析(与业务前端一致)
      '@agentscope-ai/design': fileURLToPath(
        new URL('./node_modules/@agentscope-ai/design/lib/index.js', import.meta.url),
      ),
    },
  },
  server: {
    fs: {
      allow: [path.resolve(__dirname, '../../..')],
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: false,
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    testTimeout: 15000,
    server: {
      deps: {
        inline: [/@agentscope-ai\/design/, /@agentscope-ai\/icons/],
      },
    },
  },
});
