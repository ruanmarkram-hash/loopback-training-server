import {test,expect,type APIRequestContext} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {provisionOwnedActor} from './owned-actor'
import {fixtureApi,useFixtureSession} from './helpers'

type Auth={headers:{Authorization:string}}
async function get(request:APIRequestContext,auth:Auth,path:string){
 const response=await request.get(path,auth);expect(response.status()).toBe(200);return response.json()
}

test('WEB-STATE-EFFECT001 pending inherited completion locks cancellation and repeated keyboard before exact persisted outcome',async({page,request},info)=>{
 test.setTimeout(120000)
 info.annotations.push({type:'evidence-layer',description:'actual owned plan and public HTTP; held real completion request, no fabricated response'})
 for(const [id,states] of Object.entries({'web.celebration.complete':['inflight'],'web.celebration.done':['success'],'web.modal.escape-dismiss':['busy']}))info.annotations.push({type:'control',description:JSON.stringify({id,states})})
 await new Promise(resolve=>setTimeout(resolve,13000))
 const owned=await provisionOwnedActor(request,page,'Synthetic completion busy QA')
 let planId:string|undefined,release=()=>{}
 try{
  const created=await request.post('/api/plans',{...owned.auth,data:{name:`Synthetic completion busy ${randomUUID()}`,activityType:'flexibility',status:'active',startDate:'2000-01-01',endDate:'2000-01-28'}})
  expect(created.status()).toBe(201);const plan=await created.json();planId=plan.id
  const before=await get(request,owned.auth,`/api/plans/${plan.id}`);expect(before.finishable).toBe(true)
  await page.goto(`/plans/${plan.id}`);await page.getByRole('button',{name:'Celebrate & wrap up',exact:true}).click()
  const dialog=page.getByRole('dialog',{name:'Complete plan',exact:true})
  await dialog.getByRole('button',{name:'3 of 5',exact:true}).click()
  const feedback='Synthetic completion fact café';await dialog.getByLabel('Plan feedback',{exact:true}).fill(feedback)
  const gate=new Promise<void>(resolve=>release=resolve);let posts=0
  await page.route(`**/api/plans/${plan.id}/complete`,async route=>{posts++;await gate;await route.continue()})
  const completed=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname===`/api/plans/${plan.id}/complete`)
  await dialog.getByRole('button',{name:'Complete plan',exact:true}).click()
  try{
   await expect(dialog.getByRole('button',{name:'Wrapping up…',exact:true})).toBeDisabled()
   await expect(dialog.getByRole('button',{name:'Not now',exact:true})).toBeDisabled()
   await page.keyboard.press('Escape');await expect(dialog).toBeVisible()
   await page.keyboard.press('Enter');expect(posts).toBe(1)
   expect(await get(request,owned.auth,`/api/plans/${plan.id}`)).toEqual(before)
  }finally{release()}
  expect((await completed).status()).toBe(200)
  await expect(dialog.getByRole('button',{name:'Done',exact:true})).toBeVisible()
  const saved=await get(request,owned.auth,`/api/plans/${plan.id}`)
  expect(saved.status).toBe('completed');expect(saved.metadata.completion).toMatchObject({rating:3,feedback});expect(posts).toBe(1)
  await dialog.getByRole('button',{name:'Done',exact:true}).click();await page.reload()
  expect(await get(request,owned.auth,`/api/plans/${plan.id}`)).toEqual(saved)
  await expect(page.getByRole('button',{name:'Celebrate & wrap up',exact:true})).toHaveCount(0)
 }finally{release();if(planId){const response=await request.delete(`/api/plans/${planId}`,owned.auth);expect(response.status()).toBe(204)}await owned.cleanup()}
})

test('WEB-STATE-EFFECT002 admin reset failure and busy cancellation retain exact owned identity then persist new credential',async({page,request},info)=>{
 test.setTimeout(180000)
 info.annotations.push({type:'evidence-layer',description:'public owned synthetic member/admin browser; injected503 failure only, success held real PATCH/POST and credential verified by real login'})
 for(const [id,states] of Object.entries({'web.users.reset-save':['failure','inflight','valid'],'web.users.reset-password':['valid'],'web.users.reset-cancel':['draft']}))info.annotations.push({type:'control',description:JSON.stringify({id,states})})
 const admin=fixtureApi('admin'),username=`qa-state-reset-${randomUUID()}`,password=`qa-secret-${randomUUID()}`,next=`qa-secret-${randomUUID()}`
 const created=await request.post('/api/admin/users',{...admin,data:{username,password,displayName:'Synthetic reset busy member',role:'user'}})
 expect(created.status()).toBe(201);const actor=await created.json();let release=()=>{}
 const actorRead=async()=>{const rows=await get(request,admin,'/api/admin/users');const row=rows.find((u:any)=>u.id===actor.id);expect(row).toBeTruthy();const {lastSeenAt:_lastSeen,tokenCount:_issuedLoginTokens,...facts}=row;return facts}
 const login=async(secret:string)=>{await new Promise(resolve=>setTimeout(resolve,13000));return request.post('/api/auth/login',{data:{username,password:secret,deviceName:'Synthetic reset credential verification'}})}
 try{
  const before=await actorRead();await useFixtureSession(page,'admin')
  const row=page.locator('.u-row').filter({hasText:username});await row.getByRole('button',{name:'Reset password',exact:true}).click()
  let dialog=page.getByRole('dialog',{name:'Reset password',exact:true})
  await dialog.getByLabel('New password',{exact:true}).fill(next)
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click()
  expect(await actorRead()).toEqual(before)
  expect((await login(password)).status()).toBe(200)
  await row.getByRole('button',{name:'Reset password',exact:true}).click();dialog=page.getByRole('dialog',{name:'Reset password',exact:true})
  await dialog.getByLabel('New password',{exact:true}).fill(next)
  const endpoint=`/api/admin/users/${actor.id}/password`
  await page.route(`**${endpoint}`,route=>route.fulfill({status:503,json:{detail:'Synthetic reset unavailable'}}))
  await dialog.getByRole('button',{name:'Set password',exact:true}).click()
  await expect(dialog.getByRole('alert')).toContainText('Synthetic reset unavailable')
  expect(await actorRead()).toEqual(before);expect((await login(password)).status()).toBe(200)
  await page.unroute(`**${endpoint}`)
  const gate=new Promise<void>(resolve=>release=resolve);let posts=0
  await page.route(`**${endpoint}`,async route=>{posts++;await gate;await route.continue()})
  const completed=page.waitForResponse(r=>new URL(r.url()).pathname===endpoint&&r.request().method()==='POST')
  await dialog.getByRole('button',{name:'Set password',exact:true}).click()
  try{
   await expect(dialog.getByRole('button',{name:'Saving…',exact:true})).toBeDisabled()
   await expect(dialog.getByRole('button',{name:'Cancel',exact:true})).toBeDisabled()
   await page.keyboard.press('Escape');await expect(dialog).toBeVisible();await page.keyboard.press('Enter');expect(posts).toBe(1)
   expect(await actorRead()).toEqual(before)
  }finally{release()}
  expect((await completed).status()).toBe(204);await expect(dialog).toHaveCount(0)
  expect((await login(next)).status()).toBe(200)
  expect((await login(password)).status()).toBe(401)
  await page.reload();expect(await actorRead()).toEqual(before);await expect(page.locator('.u-row').filter({hasText:username})).toContainText('Synthetic reset busy member');expect(posts).toBe(1)
 }finally{release();const retired=await request.patch(`/api/admin/users/${actor.id}`,{...admin,data:{isActive:false}});expect(retired.status()).toBe(200)}
})

test('WEB-STATE-EFFECT003 profile failure held save and injected expiry preserve supplied facts without duplicate jobs',async({page,request},info)=>{
 test.setTimeout(120000)
 info.annotations.push({type:'evidence-layer',description:'actual owned profile HTTP; injected503/401 test UI boundaries only, held realPUT verifies canonical persistence'})
 for(const [id,states] of Object.entries({'web.coach.profile-save':['failure','inflight','stored_restrictions_preserved','reload','repeated_no_duplicate_job']}))info.annotations.push({type:'control',description:JSON.stringify({id,states})})
 await new Promise(resolve=>setTimeout(resolve,13000))
 const owned=await provisionOwnedActor(request,page,'Synthetic profile mutation state QA');let release=()=>{}
 try{
  const facts={goal:{type:'general_fitness'},available_days:['mon'],timezone:'Australia/Brisbane',restrictions:['Synthetic unchanged restriction'],benchmark:{level:'established',pace_seconds_per_km:360.375,weekly_distance_meters:18000.125,long_run_meters:7000.375,source:'Synthetic exact profile state facts',observed_at:new Date().toISOString(),confidence:0.85}}
  const seeded=await request.put('/api/coaching/profile',{...owned.auth,data:facts});expect(seeded.status()).toBe(200)
  const before=await get(request,owned.auth,'/api/coaching/profile')
  await page.goto('/coach');await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue(facts.benchmark.source)
  await page.getByLabel('Benchmark source',{exact:true}).fill('Synthetic intended source change')
  await page.route('**/api/coaching/profile',route=>route.request().method()==='PUT'?route.fulfill({status:503,json:{detail:'Synthetic profile save unavailable'}}):route.continue())
  await page.getByRole('button',{name:'Save coaching profile',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('Synthetic profile save unavailable')
  expect(await get(request,owned.auth,'/api/coaching/profile')).toEqual(before)
  await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue('Synthetic intended source change')
  await page.unroute('**/api/coaching/profile')
  const jobsBefore=await get(request,owned.auth,'/api/coaching/jobs')
  const gate=new Promise<void>(resolve=>release=resolve);let puts=0
  await page.route('**/api/coaching/profile',async route=>{if(route.request().method()==='PUT'){puts++;await gate}await route.continue()})
  const completed=page.waitForResponse(r=>r.request().method()==='PUT'&&new URL(r.url()).pathname==='/api/coaching/profile')
  await page.getByRole('button',{name:'Save coaching profile',exact:true}).click()
  try{
   await expect(page.getByRole('button',{name:'Save coaching profile',exact:true})).toBeDisabled()
   await expect(page.getByLabel('Benchmark source',{exact:true})).toBeDisabled()
   await page.keyboard.press('Enter');expect(puts).toBe(1)
   expect(await get(request,owned.auth,'/api/coaching/profile')).toEqual(before)
   expect(await get(request,owned.auth,'/api/coaching/jobs')).toEqual(jobsBefore)
  }finally{release()}
  const response=await completed;expect(response.status()).toBe(200)
  const submitted=response.request().postDataJSON();expect(submitted.restrictions).toEqual(facts.restrictions)
  expect(submitted.benchmark).toEqual({...before.benchmark,source:'Synthetic intended source change'})
  await expect(page.getByRole('status').filter({hasText:'Coaching profile saved.'})).toBeVisible()
  const saved=await get(request,owned.auth,'/api/coaching/profile')
  expect(saved.restrictions).toEqual(before.restrictions);expect(saved.benchmark).toEqual({...before.benchmark,source:'Synthetic intended source change'})
  const jobsAfter=await get(request,owned.auth,'/api/coaching/jobs')
  expect(jobsAfter.filter((j:any)=>!jobsBefore.some((b:any)=>b.id===j.id))).toHaveLength(1);expect(puts).toBe(1)
  const repeated=page.waitForResponse(r=>r.request().method()==='PUT'&&new URL(r.url()).pathname==='/api/coaching/profile')
  await page.getByRole('button',{name:'Save coaching profile',exact:true}).click()
  expect((await repeated).status()).toBe(200);expect(puts).toBe(2)
  expect(await get(request,owned.auth,'/api/coaching/profile')).toEqual(saved)
  expect(await get(request,owned.auth,'/api/coaching/jobs')).toEqual(jobsAfter)
  await page.reload();await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue('Synthetic intended source change');expect(await get(request,owned.auth,'/api/coaching/profile')).toEqual(saved)
  await page.route('**/api/coaching/profile',route=>route.request().method()==='PUT'?route.fulfill({status:401,json:{detail:'Synthetic expired session'}}):route.continue())
  await page.getByLabel('Benchmark source',{exact:true}).fill('Synthetic uncommitted expired change')
  await page.getByRole('button',{name:'Save coaching profile',exact:true}).click()
  await expect(page).toHaveURL(/\/login$/)
  expect(await get(request,owned.auth,'/api/coaching/profile')).toEqual(saved)
  expect(await get(request,owned.auth,'/api/coaching/jobs')).toEqual(jobsAfter)
 }finally{release();await owned.cleanup()}
})
