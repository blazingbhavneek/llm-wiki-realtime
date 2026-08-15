import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

// VITE_HTTPS_KEY/VITE_HTTPS_CERT are filesystem paths (written by
// scripts/setup_https_livekit.sh into .env.development.local), not inline
// PEM content.
const https = process.env.VITE_HTTPS_KEY && process.env.VITE_HTTPS_CERT
  ? {
      key: readFileSync(process.env.VITE_HTTPS_KEY),
      cert: readFileSync(process.env.VITE_HTTPS_CERT),
    }
  : undefined

export default defineConfig({
  plugins: [
    tailwindcss(),
  ],
  server: {
    host: '0.0.0.0',
    https,
    proxy: {
      '/token': {
        target: process.env.VITE_TOKEN_SERVER_URL || 'http://127.0.0.1:51027',
        changeOrigin: true,
      },
    },
  },
})
