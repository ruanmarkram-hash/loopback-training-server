import { test, expect } from '@playwright/test'
import { login, useFixtureSession, accounts, screenshot } from './helpers'

// Red-capable baseline regressions, real backend. Capture before source repair.
test('WEB-A11Y-001 login fields expose associated accessible labels',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-010'},{type:'control',description:JSON.stringify({id:'web.login.username',states:['accessible_label']})},{type:'control',description:JSON.stringify({id:'web.login.password',states:['accessible_label']})})
 await page.goto('/login')
 await expect(page.getByRole('button',{name:'Sign in',exact:true})).toBeVisible()
 await expect(page.getByLabel('Username',{exact:true})).toBeVisible()
 await expect(page.getByLabel('Password',{exact:true})).toBeVisible()
})

test('WEB-A11Y-002 modal exposes dialog, Escape dismissal and restored focus',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-010'},{type:'control',description:JSON.stringify({id:'web.modal.escape-dismiss',states:['draft']})},{type:'control',description:JSON.stringify({id:'web.modal.focus',states:['open','restore']})})
 await useFixtureSession(page,'admin')
 const opener=page.getByRole('button',{name:'Create user',exact:true})
 await opener.focus()
 await page.keyboard.press('Enter')
 await screenshot(page,info.project.name,'before-create-modal-accessibility')
 await expect(page.getByRole('dialog')).toBeVisible()
 await expect(page.getByLabel('Display name',{exact:true})).toBeVisible()
 await page.keyboard.press('Escape')
 await expect(page.getByRole('dialog')).toHaveCount(0)
 await expect(opener).toBeFocused()
})

test('AUTH-008 account switch never exposes athlete A cached workout rows to empty athlete B',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'AUTH-008'},{type:'control',description:JSON.stringify({id:'web.layout.logout',states:['athlete','cross_account_loading_isolation']})})
 await login(page,'athlete')
 await page.goto('/workouts')
 await expect(page.locator('.wo-row').first()).toBeVisible()
 if(await page.getByRole('button',{name:'Menu',exact:true}).isVisible()) await page.getByRole('button',{name:'Menu',exact:true}).click()
 await page.getByTitle('Log out',{exact:true}).click()
 await expect(page).toHaveURL(/\/login$/)
 await page.locator('input[autocomplete="username"]').fill(accounts.athleteB.username)
 await page.locator('input[autocomplete="current-password"]').fill(accounts.athleteB.password)
 await page.getByRole('button',{name:'Sign in',exact:true}).click()
 await expect(page).not.toHaveURL(/\/login$/)
 // Hold B's response so the assertion can inspect transitional rendering.
 // A's cache must not be shown even temporarily while B's data is loading.
 let release!:()=>void
 const gate=new Promise<void>(resolve=>release=resolve)
 await page.route('**/api/workouts?**',async route=>{await gate;await route.continue()})
 try{
  if(await page.getByRole('button',{name:'Menu',exact:true}).isVisible()) await page.getByRole('button',{name:'Menu',exact:true}).click()
  await page.getByRole('link',{name:'Workouts',exact:true}).click()
  await expect(page).toHaveURL(/\/workouts$/)
  await screenshot(page,info.project.name,'before-account-switch-isolation')
  await expect(page.locator('.wo-row')).toHaveCount(0)
 }finally{release()}
 await expect(page.getByText('No workouts yet',{exact:true})).toBeVisible()
})
