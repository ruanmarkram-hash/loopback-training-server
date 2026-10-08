import {test,expect} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {useFixtureSession,fixtureApi,screenshot} from './helpers'

test('WEB-ADMIN-001 create validation cancel identity reset revoke deactivate reactivate persistence',async({page,request},info)=>{
 test.setTimeout(90_000)
 for(const id of ['UI-002','UI-003','UI-004','UI-005','UI-014','AUTH-007'])info.annotations.push({type:'scenario',description:id})
 const coverage = {
  'web.users.create-open':['default'],'web.users.create-display-name':['unicode'],'web.users.create-username':['blank','whitespace','uppercase','duplicate','valid'],'web.users.create-password':['short','valid'],'web.users.create-cancel':['draft'],'web.users.create-save':['valid','failure'],
  'web.users.reset-open':['first'],'web.users.reset-password':['short','valid'],'web.users.reset-cancel':['draft'],'web.users.reset-save':['valid'],
  'web.users.tokens-expand':['populated'],'web.users.tokens-collapse':['expanded'],'web.users.token-revoke-open':['populated'],'web.users.token-revoke-cancel':['confirm'],'web.users.token-revoke-confirm':['confirm'],
  'web.users.deactivate-open':['other-active','self-hidden'],'web.users.deactivate-cancel':['confirm'],'web.users.deactivate-confirm':['confirm'],'web.users.reactivate':['inactive','failure']
 }
 for(const [id,states] of Object.entries(coverage))info.annotations.push({type:'control',description:JSON.stringify({id,states})})
 const admin=fixtureApi('admin'), suffix=randomUUID().slice(0,8), username=`synthetic-web-${suffix}`, display=`Synthetic café ${suffix}`, password=`qa-secret-${randomUUID()}`, next=`qa-secret-${randomUUID()}`
 const users=async()=>{const response=await request.get('/api/admin/users',admin);expect(response.ok()).toBeTruthy();return (await response.json()).map((user: Record<string,unknown>) => {const {lastSeenAt: _activityTimestamp, ...persistent} = user;return persistent})}
 const initial=await users()
 await useFixtureSession(page,'admin')
 const selfRow=page.locator('.u-row').filter({hasText:'loopback-admin'})
 await expect(selfRow.getByRole('button',{name:'Deactivate',exact:true})).toHaveCount(0)
 await page.getByRole('button',{name:'Create user',exact:true}).click()
 let dialog=page.getByRole('dialog',{name:'Create member',exact:true}), save=dialog.getByRole('button',{name:'Create member',exact:true})
 await expect(save).toBeDisabled();await dialog.getByLabel('Username',{exact:true}).fill('bad name');await dialog.getByLabel('Password',{exact:true}).fill(password);await expect(save).toBeDisabled()
 await dialog.getByLabel('Username',{exact:true}).fill(username.toUpperCase());await dialog.getByLabel('Display name',{exact:true}).fill(display);await dialog.getByLabel('Password',{exact:true}).fill('short');await expect(save).toBeDisabled()
 await dialog.getByLabel('Password',{exact:true}).fill(password);await expect(save).toBeEnabled();await dialog.getByRole('button',{name:'Cancel',exact:true}).click();expect(await users()).toEqual(initial)
 await page.getByRole('button',{name:'Create user',exact:true}).click();dialog=page.getByRole('dialog',{name:'Create member',exact:true})
 await dialog.getByLabel('Username',{exact:true}).fill(username.toUpperCase());await dialog.getByLabel('Display name',{exact:true}).fill(display);await dialog.getByLabel('Password',{exact:true}).fill(password)
 await page.route('**/api/admin/users',async route=>{if(route.request().method()==='POST')await route.fulfill({status:500,json:{detail:'Synthetic create failure'}});else await route.continue()})
 await dialog.getByRole('button',{name:'Create member',exact:true}).click();await expect(dialog.getByRole('alert')).toHaveText(/Synthetic create failure/);expect(await users()).toEqual(initial)
 await page.unroute('**/api/admin/users');await dialog.getByRole('button',{name:'Create member',exact:true}).click();await expect(dialog).toHaveCount(0);await page.reload()
 let row=page.locator('.u-row').filter({hasText:username});await expect(row).toContainText(display)
 const created=(await users()).find((u:{username:string})=>u.username===username);expect(created).toMatchObject({username,displayName:display,role:'user',isActive:true})
 expect((await users()).filter((u:{id:string})=>u.id!==created.id)).toEqual(initial)
 await page.getByRole('button',{name:'Create user',exact:true}).click();dialog=page.getByRole('dialog',{name:'Create member',exact:true});await dialog.getByLabel('Username',{exact:true}).fill(username);await dialog.getByLabel('Password',{exact:true}).fill(password);await dialog.getByRole('button',{name:'Create member',exact:true}).click();await expect(dialog.getByRole('alert')).toBeVisible();expect((await users()).filter((u:{username:string})=>u.username===username)).toHaveLength(1);await dialog.getByRole('button',{name:'Cancel',exact:true}).click()
 await row.getByRole('button',{name:'Reset password',exact:true}).click();dialog=page.getByRole('dialog',{name:'Reset password',exact:true});await dialog.getByLabel('New password',{exact:true}).fill('short');await expect(dialog.getByRole('button',{name:'Set password',exact:true})).toBeDisabled();await dialog.getByLabel('New password',{exact:true}).fill(next);await dialog.getByRole('button',{name:'Cancel',exact:true}).click()
 const login=await request.post('/api/auth/login',{data:{username,password,deviceName:`Synthetic token ${suffix}`}});expect(login.status()).toBe(200);const session=await login.json(), memberAuth={headers:{Authorization:`Bearer ${session.token}`}}
 await row.getByRole('button',{name:'Reset password',exact:true}).click();dialog=page.getByRole('dialog',{name:'Reset password',exact:true});await dialog.getByLabel('New password',{exact:true}).fill(next);await dialog.getByRole('button',{name:'Set password',exact:true}).click();await expect(dialog).toHaveCount(0)
 expect((await request.get('/api/auth/me',memberAuth)).status()).toBe(200)
 const relogin=await request.post('/api/auth/login',{data:{username,password:next,deviceName:`Synthetic keep ${suffix}`}});expect(relogin.status()).toBe(200);const second=await relogin.json()
 await page.reload();row=page.locator('.u-row').filter({hasText:username});await row.getByTitle("Show this member's tokens").click()
 let panel=row.locator('..').locator('.u-tokens-panel');await expect(panel).toContainText(`Synthetic token ${suffix}`)
 await row.getByTitle("Show this member's tokens").click();await expect(panel).toHaveCount(0);await row.getByTitle("Show this member's tokens").click();panel=row.locator('..').locator('.u-tokens-panel')
 const tokenRow=panel.locator('.token-row').filter({hasText:`Synthetic token ${suffix}`});await tokenRow.getByRole('button',{name:'Revoke',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'Cancel',exact:true}).click();expect((await request.get('/api/auth/me',memberAuth)).status()).toBe(200)
 await tokenRow.getByRole('button',{name:'Revoke',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'Revoke',exact:true}).click();await expect(dialog).toHaveCount(0);expect((await request.get('/api/auth/me',memberAuth)).status()).toBe(401)
 await row.getByRole('button',{name:'Deactivate',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'Cancel',exact:true}).click();expect((await users()).find((u:{id:string})=>u.id===created.id).isActive).toBe(true)
 await row.getByRole('button',{name:'Deactivate',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'Deactivate',exact:true}).click();await expect(dialog).toHaveCount(0);await page.reload();row=page.locator('.u-row').filter({hasText:username});await expect(row.getByRole('button',{name:'Reactivate',exact:true})).toBeVisible();expect((await users()).find((u:{id:string})=>u.id===created.id).isActive).toBe(false)
 expect((await request.get('/api/auth/me',{headers:{Authorization:`Bearer ${second.token}`}})).status()).toBe(401)
 await page.route(`**/api/admin/users/${created.id}`,route=>route.fulfill({status:500,json:{detail:'Synthetic reactivate failure'}}));await row.getByRole('button',{name:'Reactivate',exact:true}).click();await expect(page.getByRole('alert')).toHaveText(/Synthetic reactivate failure/);expect((await users()).find((u:{id:string})=>u.id===created.id).isActive).toBe(false)
 await screenshot(page,info.project.name,'admin-reactivation-error');await page.unroute(`**/api/admin/users/${created.id}`);await row.getByRole('button',{name:'Reactivate',exact:true}).click();await expect(row.getByRole('button',{name:'Deactivate',exact:true})).toBeVisible();await page.reload();expect((await users()).find((u:{id:string})=>u.id===created.id).isActive).toBe(true)
 await request.patch(`/api/admin/users/${created.id}`,{...admin,data:{isActive:false}})
})
