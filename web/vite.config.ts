// `vitest/config` re-exports Vite's `defineConfig` with the `test` block typed.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// The dashboard is served two ways: `npm run dev` proxies the API to a running
// backend, and `npm run build` emits `dist/` which FastAPI serves from the same
// origin (so no CORS, no second server in production).
//
// The dev proxy target is an env var because the backend moves: `mi proxy`
// defaults to :8765 on the host, while `docker compose up` serves :8000. Point
// it at either with `MI_API_TARGET=http://127.0.0.1:8000 npm run dev`.
const apiTarget = process.env.MI_API_TARGET || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // Explicit IPv4 loopback: the default `localhost` can bind IPv6-only on
    // macOS, so http://127.0.0.1:5173 would refuse while localhost works.
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      '/v1': apiTarget,
      '/healthz': apiTarget,
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
  test: {
    // jsdom, because the markdown component is only meaningful once rendered —
    // asserting on a tree of React elements would not catch a dropped `<table>`.
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
})
