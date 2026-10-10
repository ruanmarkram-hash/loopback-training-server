import { expect } from '@playwright/test'

export function auditMemberNames(rows: Element[]) {
  return rows.map(row => {
    const name = row.querySelector(':scope > .u-member > div > .u-member-name')
    const token = row.querySelector(':scope > .u-tokens-toggle')
    if (!name || !token) throw new Error('Member name or token control missing from row')
    const range = document.createRange()
    range.selectNodeContents(name)
    return {
      rowId: row.getAttribute('data-user-id'),
      nameRight: range.getBoundingClientRect().right,
      tokenLeft: token.getBoundingClientRect().left,
    }
  })
}

export function expectMemberNamesDoNotOverlap(audit: ReturnType<typeof auditMemberNames>) {
  expect(audit.length).toBeGreaterThan(0)
  for (const row of audit) expect(row.nameRight, `Member row ${row.rowId}`).toBeLessThanOrEqual(row.tokenLeft - 1)
}
