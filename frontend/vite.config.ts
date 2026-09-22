import { defineConfig } from 'vitest/config'
import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig(({ mode }) => {
  // 加载 .env 文件中的环境变量，'' 前缀表示加载所有变量（包括 VITE_ 前缀）
  const env = loadEnv(mode, process.cwd(), '')
  const API_TARGET = env.VITE_API_TARGET || 'http://localhost:8000'

  return {
    base: '/vllm_manager/frontend/',
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 5178,
      proxy: {
        '/vllm_manager/web_api': {
          target: API_TARGET,
          changeOrigin: true,
          ws: true,
        },
        '/vllm_manager/api': {
          target: API_TARGET,
          changeOrigin: true,
          ws: true,
        },
      },
    },
    test: {
      globals: true,
      environment: 'happy-dom',
      setupFiles: './src/test-setup.ts',
      include: ['src/**/*.test.{ts,tsx}'],
    },
  }
})
