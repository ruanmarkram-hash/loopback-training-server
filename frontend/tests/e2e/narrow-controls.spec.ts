import {test,expect} from '@playwright/test'
import {useFixtureSession,screenshot} from './helpers'
test('WEB-NARROW-001 queue deletion controls remain inside narrow viewport',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-008'},{type:'control',description:JSON.stringify({id:'web.queue.delete-open',states:['narrow_reachable']})})
 await useFixtureSession(page,'athlete');await page.goto('/queue');await expect(page.locator('.queue-card').first()).toBeVisible()
 await screenshot(page,info.project.name,'queue-control-reachability')
 const buttons=page.getByTitle('Delete queue item',{exact:true});expect(await buttons.count()).toBeGreaterThan(0)
 for(const button of await buttons.all()){const box=await button.boundingBox();expect(box).not.toBeNull();expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(page.viewportSize()!.width)}
})
test('WEB-NARROW-002 member name text never overlaps token controls',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-008'},{type:'control',description:JSON.stringify({id:'web.users.tokens-expand',states:['narrow_nonoverlap']})})
 await useFixtureSession(page,'admin');await expect(page.locator('.u-row').first()).toBeVisible();await screenshot(page,info.project.name,'admin-member-token-layout')
 const audit=await page.locator('.u-row').evaluateAll(rows=>rows.map(row=>{const name=row.querySelector('div > div > div'),token=row.querySelector('.u-tokens-toggle');if(!name||!token)return null;const range=document.createRange();range.selectNodeContents(name);return {nameRight:range.getBoundingClientRect().right,tokenLeft:token.getBoundingClientRect().left}}).filter(Boolean))
 expect(audit.length).toBeGreaterThan(0);for(const row of audit)expect(row!.nameRight).toBeLessThanOrEqual(row!.tokenLeft-1)
})
