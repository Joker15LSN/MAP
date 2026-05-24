import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: ['host.docker.internal', 'localhost', '127.0.0.1'],
    proxy: {
      '/api': {
        target:
          process.env.VITE_MAP_BFF_API_ORIGIN || 'http://localhost:18080',
        changeOrigin: true,
      },
    },
  },
});
