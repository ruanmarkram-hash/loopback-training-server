import { readFileSync } from 'node:fs'
import { test, expect } from '@playwright/test'
import { auditMemberNames, expectMemberNamesDoNotOverlap } from './member-name-audit'

// A retained pre-repair stylesheet supplies causal RED evidence without changing product files.
const css = readFileSync(process.env.LOOPBACK_MEMBER_LAYOUT_CSS ?? new URL('../../src/styles/settings.css', import.meta.url), 'utf8')
const longName = 'LongMemberName'.repeat(12)
const markup = `<div class="users-table"><div><div class="u-row" data-user-id="synthetic-long-name">
  <div class="u-member" style="display:flex;align-items:center;gap:12px;min-width:0">
    <span class="avatar-lg" style="width:38px;height:38px">L</span>
    <div style="min-width:0"><div class="u-member-name">${longName}</div><div>${longName}</div></div>
  </div><span class="hide-sm">Member</span><button class="u-tokens-toggle">5</button>
  <span class="hide-sm">never</span><span>Active</span><div class="u-actions">Actions</div>
</div></div></div>`

for (const width of [320, 430, 768, 1440]) {
  test(`member names wrap before token controls at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.setContent(`<style>${css} body{margin:0;font:14px sans-serif}</style>${markup}`)
    const row = page.locator('.u-row')
    expect(await row.evaluate(element => element.querySelector('div > div > div')?.className)).toBe('u-member')
    expect(await row.evaluate(element => element.querySelector(':scope > .u-member > div > .u-member-name')?.className)).toBe('u-member-name')
    if (process.env.LOOPBACK_MEMBER_LAYOUT_EVIDENCE) await page.screenshot({ path: `${process.env.LOOPBACK_MEMBER_LAYOUT_EVIDENCE}-${width}.png`, fullPage: true })
    expectMemberNamesDoNotOverlap(await row.evaluateAll(auditMemberNames))
  })
}

test('the same name audit rejects deliberately overlapping text', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.setContent(`<style>${css} body{margin:0;font:14px sans-serif}.u-member-name{white-space:nowrap;overflow-wrap:normal}</style>${markup}`)
  const audit = await page.locator('.u-row').evaluateAll(auditMemberNames)
  expect(audit[0].nameRight).toBeGreaterThan(audit[0].tokenLeft)
  expect(() => expectMemberNamesDoNotOverlap(audit)).toThrow()
})
