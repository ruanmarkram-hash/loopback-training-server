import {defineConfig} from '@playwright/test'
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '../../../artifacts/web-qa/browsers'
process.env.PLAYWRIGHT_SKIP_BROWSER_GC='1'
process.env.LOOPBACK_QA_EVIDENCE_LAYER='mocked_web_transport'
process.env.LOOPBACK_QA_ORIGIN='http://127.0.0.1:18094'
export default defineConfig({testDir:'./tests/e2e',testMatch:['coaching-review.spec.ts','semantic-layout.spec.ts'],workers:1,retries:0,timeout:30000,expect:{timeout:5000},reporter:[['./tests/e2e/safe-reporter.ts']],use:{baseURL:'http://127.0.0.1:18094',timezoneId:'Australia/Brisbane',trace:'off',screenshot:'off',video:'off',browserName:'chromium',viewport:{width:1440,height:900}},projects:[{name:'mock-review-chromium-1440'},{name:'mock-review-webkit-390',use:{browserName:'webkit',viewport:{width:390,height:844},isMobile:true,hasTouch:true}}]})
