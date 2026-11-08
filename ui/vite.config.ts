import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // The UI calls the API on its own origin (see src/api.ts) - which, packaged, is the
    // backend serving the built UI. In development, forward those calls to the backend.
    proxy: { '/api': 'http://localhost:8000' },
  },
})
