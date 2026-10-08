import {test,expect}from '@playwright/test'
import {useFixtureSession,screenshot,artifactRoot}from './helpers'
import {writeFileSync}from 'node:fs';import path from 'node:path'
test('WEB-LAYOUT-001 inherited coach notes must fit viewport without document scroll',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-008'})
 await useFixtureSession(page,'athlete')
 await page.goto('/notes')
 await expect(page.locator('.coach-panel .cp-title')).toBeVisible()
 await expect(page.locator('.wo-row')).toHaveCount(0)
 const audit=await page.evaluate(()=>({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,overflow:Array.from(document.querySelectorAll('body *')).map(el=>({tag:el.tagName,class:el.className,left:el.getBoundingClientRect().left,right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width,scroll:el.scrollWidth,client:el.clientWidth})).filter(el=>el.right>document.documentElement.clientWidth+1&&el.width>0)}))
 writeFileSync(path.join(artifactRoot,`overflow-${info.project.name}.json`),JSON.stringify(audit,null,2))
 await screenshot(page,info.project.name,'before-overflow')
 expect(audit.scroll).toBeLessThanOrEqual(audit.width+1)
})
