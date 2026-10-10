import { test, expect } from '@playwright/test'
import { useFixtureSession, runtimeInventory, screenshot, expectNoOverflow } from './helpers'

// Captures the original untouched server image. These assertions qualify render/layout
// only, never imply that every captured control performed its action.
test('BASE-WEB-001 unauthenticated routing and blank login baseline', async ({page},info)=>{
 await page.goto('/plans')
 await expect(page).toHaveURL(/\/login$/)
 await expect(page.getByRole('button',{name:'Sign in',exact:true})).toBeVisible()
 await runtimeInventory(page,info.project.name,'login')
 await screenshot(page,info.project.name,'baseline-login')
 await expectNoOverflow(page,true)
})

for(const role of ['athlete','admin'] as const){
 test(`BASE-WEB-${role} inherited route inventory and baseline screenshots`,async({page},info)=>{
  test.setTimeout(120_000)
  const errors:string[]=[]
  page.on('pageerror',error=>errors.push(error.message))
  await useFixtureSession(page,role)
  const routes=role==='admin'?['/users','/system','/settings']:['/','/calendar','/workouts','/plans','/notes','/health','/queue','/settings']
  for(const route of routes){
   await page.goto(route)
   await expect(page.locator('.page-title')).not.toBeEmpty()
   await expect(page.locator('.spinner')).toHaveCount(0)
   const markers:Record<string,string>={'/':'.plan-hero','/calendar':'.cal-month-card','/workouts':'.wo-row','/plans':'.plan-card','/notes':'.cp-title','/health':'.chart-hover','/queue':'.q-title','/settings':'.profile-card','/users':'.u-row','/system':'.system-wrap'}
   if(markers[route])await expect(page.locator(markers[route]).first()).toBeVisible()
   const name=route==='/'?'overview':route.slice(1)
   await runtimeInventory(page,info.project.name,`${role}-${name}`)
   await screenshot(page,info.project.name,`baseline-${role}-${name}`)
   await expectNoOverflow(page,true)
   if(route==='/workouts' && await page.locator('.wo-row').count()){
    await page.locator('.wo-row').first().click()
    await expect(page).toHaveURL(/\/workouts\/[^/]+$/)
    await expect(page.locator('.det-stats')).toBeVisible()
    await expect(page.locator('.spinner')).toHaveCount(0)
    await runtimeInventory(page,info.project.name,'workout-detail')
    await screenshot(page,info.project.name,'baseline-workout-detail')
    await expectNoOverflow(page,true)
   }
   if(route==='/plans' && await page.locator('.plan-card').count()){
    const selectedPlanName=await page.locator('.plan-card .pc-name').first().textContent()
    await page.locator('.plan-card').first().click()
    await expect(page).toHaveURL(/\/plans\/[^/]+$/)
    await expect(page.locator('.page-title')).toHaveText(selectedPlanName!)
    await expect(page.locator('.spinner')).toHaveCount(0)
    await runtimeInventory(page,info.project.name,'plan-detail')
    await screenshot(page,info.project.name,'baseline-plan-detail')
    await expectNoOverflow(page,true)
   }
  }
  expect(errors,'unhandled browser exceptions').toEqual([])
 })
}
