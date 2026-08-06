import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

const https = process.env.VITE_HTTPS_KEY && process.env.VITE_HTTPS_CERT
  ? {
      key: process.env.VITE_HTTPS_KEY,
      cert: process.env.VITE_HTTPS_CERT,
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
