process.env.PLAYWRIGHT_BROWSERS_PATH ||= '../../../artifacts/web-qa/browsers'
process.env.PLAYWRIGHT_SKIP_BROWSER_GC = '1'
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  testIgnore: '**/coaching-review.spec.ts', // Mock transport cases run only under the separate review config.
  globalSetup: './tests/e2e/fixture-setup.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  outputDir: '../../../artifacts/web-qa/results',
  reporter: [['./tests/e2e/safe-reporter.ts']],
  use: {
    baseURL: process.env.LOOPBACK_QA_ORIGIN || 'http://127.0.0.1:18089',
    timezoneId: 'Australia/Brisbane',
    locale: 'en-AU',
    trace: 'off', // Login and minted-token responses contain credentials.
    screenshot: 'off', // Capture reviewed explicit screenshots after credential entry only.
    video: 'off',
  },
  projects: [
    { name: 'chromium-1440', use: { browserName: 'chromium', viewport: { width: 1440, height: 1000 } } },
    { name: 'webkit-1280', use: { browserName: 'webkit', viewport: { width: 1280, height: 900 } } },
    { name: 'firefox-1920', use: { browserName: 'firefox', viewport: { width: 1920, height: 1080 } } },
    { name: 'chromium-mobile-narrow', use: { browserName: 'chromium', viewport: { width: 320, height: 640 }, isMobile: true, hasTouch: true } },
    { name: 'webkit-mobile-typical', use: { browserName: 'webkit', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
    { name: 'chromium-mobile-large', use: { browserName: 'chromium', viewport: { width: 430, height: 932 }, isMobile: true, hasTouch: true } },
    { name: 'webkit-landscape', use: { browserName: 'webkit', viewport: { width: 844, height: 390 }, isMobile: true, hasTouch: true } },
  ],
})
