import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: the Vite server proxies /api to uvicorn so the browser sees one
// origin in both modes (spec §2 -- no CORS configuration anywhere).
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
