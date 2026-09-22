import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/static/vue/',
  build: { outDir: '../src/ai_rpg/api/static/vue', emptyOutDir: true },
  test: { environment: 'jsdom', restoreMocks: true },
})
