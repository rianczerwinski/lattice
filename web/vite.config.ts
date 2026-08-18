import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [svelte()],
  server: {
    host: '127.0.0.1',
    port: 5178,
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:9010' },
  },
  build: { target: 'es2022' },
});
