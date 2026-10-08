import { test, expect, type Page, type APIRequestContext } from '@playwright/test'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

export const artifactRoot = path.resolve('../../../artifacts/web-qa')
export const candidateFixture = process.env.LOOPBACK_QA_FIXTURE === 'candidate'
export const sessionFile = path.join(artifactRoot,candidateFixture ? 'private-candidate-sessions.json' : 'private-sessions.json')
export const accountFile = path.join(artifactRoot,candidateFixture ? 'private-candidate-accounts.json' : 'private-accounts.json')
const env = Object.fromEntries(readFileSync(path.resolve(candidateFixture ? '../../../artifacts/baseline-server/candidate/private.env' : '../../../artifacts/baseline-server/private.env'), 'utf8').split(/\r?\n/).filter(l => l && !l.startsWith('#')).map(l => { const i=l.indexOf('='); return [l.slice(0,i),l.slice(i+1).replace(/^['"]|['"]$/g,'')] }))
export const accounts = { get athleteB() {const accountB=JSON.parse(readFileSync(accountFile,'utf8'));return {username:accountB.username,password:accountB.password}}, admin: { username: 'loopback-admin', password: env.BOOTSTRAP_ADMIN_PASSWORD }, athlete: { username: 'loopback-fixture', password: env.ATHLETE_PASSWORD } }
export async function login(page: Page, role: keyof typeof accounts) {
 await page.goto('/login')
 await expect(page.getByRole('button',{name:'Sign in',exact:true})).toBeVisible()
 await page.locator('input[autocomplete="username"]').fill(accounts[role].username)
 await page.locator('input[autocomplete="current-password"]').fill(accounts[role].password)
 await page.getByRole('button',{name:'Sign in',exact:true}).click()
 await expect(page).not.toHaveURL(/\/login$/)
 await expect(page.locator('.page-title')).toHaveText(role==='admin'?'User management':role==='athlete'?'Hey Sofia':'Hey Synthetic')
}
export async function apiSession(request: APIRequestContext, role: keyof typeof accounts) {
 const res=await request.post('/api/auth/login',{data:{...accounts[role],deviceName:'Synthetic web QA API'}})
 if(!res.ok()) throw new Error(`Synthetic API login failed (${res.status()})`)
 const data=await res.json()
 return { headers: { Authorization: `Bearer ${data.token}` }, tokenId:data.tokenId }
}
export async function screenshot(page: Page, project: string, name: string) {
 const dir=path.join(artifactRoot,'screenshots',project);mkdirSync(dir,{recursive:true})
 const file=path.join(dir,`${process.env.LOOPBACK_QA_SCREENSHOT_STAGE || 'capture'}-${name}.png`)
 await page.screenshot({path:file,fullPage:true,animations:'disabled'})
 await test.info().attach(name,{path:file,contentType:'image/png'})
}
export async function runtimeInventory(page: Page, project: string, name: string) {
 const controls=await page.locator('button,input,select,textarea,a,[role="button"],[tabindex]').evaluateAll(elements=>elements.map((el)=>({tag:el.tagName.toLowerCase(),role:el.getAttribute('role'),name:el.getAttribute('aria-label')||el.getAttribute('title')||(el instanceof HTMLInputElement ? el.getAttribute('autocomplete') : el.textContent?.trim().replace(/\s+/g,' ').slice(0,180)),testId:el.getAttribute('data-testid'),disabled:el.hasAttribute('disabled'),type:el.getAttribute('type'),visible:el.getBoundingClientRect().width>0&&el.getBoundingClientRect().height>0})))
 const dir=path.join(artifactRoot,'runtime',project);mkdirSync(dir,{recursive:true});writeFileSync(path.join(dir,`${name}.json`),JSON.stringify({route:new URL(page.url()).pathname,controls},null,2))
}
export async function expectNoOverflow(page: Page, soft = false) {
 const {width,scroll}=await page.evaluate(()=>({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}))
 const assertion = soft ? expect.soft : expect
 assertion(scroll,'document horizontal overflow').toBeLessThanOrEqual(width+1)
}

export async function useFixtureSession(page:Page,role:keyof typeof accounts){
 const sessions=JSON.parse(readFileSync(sessionFile,'utf8'))
 const session=sessions[role]
 await page.addInitScript(({token,tokenId,user})=>{
  localStorage.setItem('loopback.token',token);localStorage.setItem('loopback.tokenId',tokenId);localStorage.setItem('loopback.user',JSON.stringify(user))
 },session)
 await page.goto(role==='admin'?'/users':'/')
 await expect(page.locator('.page-title')).toHaveText(role==='admin'?'User management':role==='athlete'?'Hey Sofia':'Hey Synthetic')
}

export function fixtureApi(role:keyof typeof accounts){
 const sessions=JSON.parse(readFileSync(sessionFile,'utf8'))
 return {headers:{Authorization:`Bearer ${sessions[role].token}`}}
}
