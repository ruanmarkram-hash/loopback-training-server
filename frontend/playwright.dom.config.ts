import { defineConfig } from '@playwright/test'

// Offline DOM layout regression: no app server, login fixtures or transport.
export default defineConfig({
  testDir: './tests/e2e',
  testMatch: 'narrow-controls-dom.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 15_000,
  reporter: 'line',
  use: { browserName: 'chromium', trace: 'off', screenshot: 'off', video: 'off' },
})
