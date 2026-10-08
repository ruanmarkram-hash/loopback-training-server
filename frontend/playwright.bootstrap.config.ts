import {defineConfig} from '@playwright/test'
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '../../../artifacts/web-qa/browsers'
process.env.PLAYWRIGHT_SKIP_BROWSER_GC='1'
export default defineConfig({testDir:'./tests/e2e',testMatch:'bootstrap.spec.ts',workers:1,retries:0,timeout:60000,expect:{timeout:8000},reporter:[['./tests/e2e/safe-reporter.ts']],use:{baseURL:process.env.LOOPBACK_QA_ORIGIN||'http://127.0.0.1:18095',timezoneId:'Australia/Brisbane',locale:'en-AU',trace:'off',screenshot:'off',video:'off',browserName:'chromium',viewport:{width:320,height:640}},projects:[{name:'bootstrap-chromium-320'}]})
