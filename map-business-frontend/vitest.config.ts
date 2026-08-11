import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // @agentscope-ai/design 未声明 exports/main,仅 module 字段,
      // 直接指向其 ESM 入口供 vitest 解析
      '@agentscope-ai/design': fileURLToPath(
        new URL('./node_modules/@agentscope-ai/design/lib/index.js', import.meta.url),
      ),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: false,
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    testTimeout: 10000,
    server: {
      deps: {
        // @agentscope-ai/design 仅声明 module 字段,需显式内联其 ESM 依赖图
        inline: [/@agentscope-ai\/design/, /@agentscope-ai\/icons/],
      },
    },
  },
});
