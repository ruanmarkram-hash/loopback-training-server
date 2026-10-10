import {test,expect} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {fixtureApi,useFixtureSession,screenshot,expectNoOverflow} from './helpers'
test('WEB-COACH-007 new actor with retrieved null profile can begin onboarding',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'ADAPT-002'},{type:'control',description:JSON.stringify({id:'web.coach.goal',states:['retrieved_empty_profile_enabled']})})
 const admin=fixtureApi('admin'),username=`web-onboard-${randomUUID().slice(0,8)}`,password=`qa-secret-${randomUUID()}`
 const created=await request.post('/api/admin/users',{...admin,data:{username,password,displayName:'Synthetic onboarding QA',role:'user'}});expect(created.status()).toBe(201);const actor=await created.json()
 try{const login=await request.post('/api/auth/login',{data:{username,password,deviceName:'Synthetic onboarding QA'}});expect(login.ok()).toBeTruthy();const session=await login.json();const auth={headers:{Authorization:`Bearer ${session.token}`}};const profile=await request.get('/api/coaching/profile',auth);expect(await profile.json()).toBeNull()
 await page.addInitScript(({token,tokenId,user})=>{localStorage.setItem('loopback.token',token);localStorage.setItem('loopback.tokenId',tokenId);localStorage.setItem('loopback.user',JSON.stringify(user))},session);await page.goto('/coach');await expect(page.getByRole('button',{name:'Refresh status',exact:true})).toBeEnabled();await expect(page.getByLabel('Goal',{exact:true})).toBeEnabled();await page.getByLabel('Goal',{exact:true}).selectOption('first_5k');await expect(page.getByLabel('Event date',{exact:true})).toHaveValue('');await expect(page.getByText('No event date selected.',{exact:true})).toBeVisible();await expect(page.getByRole('checkbox',{name:'I am a beginner and want a conservative walk/run start'})).toBeVisible();expect(await (await request.get('/api/coaching/profile',auth)).json()).toBeNull();await expectNoOverflow(page);await screenshot(page,info.project.name,'coaching-new-actor-onboarding')
 }finally{await request.patch(`/api/admin/users/${actor.id}`,{...admin,data:{isActive:false}})}
})
test('WEB-COACH-008 structured validation explains confidence error without changing stored facts',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'UI-007'},{type:'control',description:JSON.stringify({id:'web.coach.benchmark-confidence',states:['invalid_server_explanation','no_invalid_mutation']})})
 await useFixtureSession(page,'athleteB');const auth=fixtureApi('athleteB'),before=await (await request.get('/api/coaching/profile',auth)).json();await page.goto('/coach');await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue(before.benchmark.source)
 await page.getByLabel('Benchmark confidence (optional, 0–1)',{exact:true}).fill('2');const response=page.waitForResponse(r=>r.url().endsWith('/api/coaching/profile')&&r.request().method()==='PUT');await page.getByRole('button',{name:'Save coaching profile',exact:true}).click();expect((await response).status()).toBe(422);await expect(page.getByRole('alert')).toContainText('confidence');await expect(page.getByRole('alert')).toContainText('less than or equal to 1');expect(await (await request.get('/api/coaching/profile',auth)).json()).toEqual(before);await expectNoOverflow(page);await screenshot(page,info.project.name,'coaching-structured-field-error')
})
test('WEB-COACH-009 goal and beginner touch targets meet the existing 44px coaching standard',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-011'},{type:'control',description:JSON.stringify({id:'web.coach.goal',states:['minimum_target_size']})},{type:'control',description:JSON.stringify({id:'web.coach.beginner',states:['minimum_target_size']})})
 await useFixtureSession(page,'athleteB');await page.goto('/coach');const goal=page.getByLabel('Goal',{exact:true});await expect(goal).toBeEnabled();const goalBounds=await goal.boundingBox();expect(goalBounds?.height).toBeGreaterThanOrEqual(44)
 await goal.selectOption('first_5k');const label=page.locator('label').filter({has:page.getByRole('checkbox',{name:'I am a beginner and want a conservative walk/run start'})});const bounds=await label.boundingBox();expect(bounds?.height).toBeGreaterThanOrEqual(44)
})
test('WEB-COACH-010 initial start value allows a draft preview without same-day rejection',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'ADAPT-002'},{type:'control',description:JSON.stringify({id:'web.coach.start-date',states:['default_future_preview']})})
 const admin=fixtureApi('admin'),sharedAuth=fixtureApi('athleteB')
 const sharedResponse=await request.get('/api/coaching/profile',sharedAuth);expect(sharedResponse.status()).toBe(200);const sharedBefore=await sharedResponse.json()
 const username=`web-default-start-${randomUUID().slice(0,8)}`,password=`qa-secret-${randomUUID()}`
 let actorId:string|undefined,draftId:string|undefined,auth:{headers:{Authorization:string}}|undefined
 try{
  const created=await request.post('/api/admin/users',{...admin,data:{username,password,displayName:'Synthetic default start QA',role:'user'}});expect(created.status()).toBe(201);actorId=(await created.json()).id
  const login=await request.post('/api/auth/login',{data:{username,password,deviceName:'Synthetic default start QA'}});expect(login.status()).toBe(200);const session=await login.json();auth={headers:{Authorization:`Bearer ${session.token}`}}
  const profile={goal:{type:'general_fitness'},available_days:['mon','wed','fri'],timezone:'Australia/Brisbane',restrictions:[],benchmark:{level:'established',pace_seconds_per_km:420,weekly_distance_meters:10000,long_run_meters:5000,source:'Synthetic default start fixture',observed_at:new Date(Date.now()-86400000).toISOString(),confidence:1}}
  const saved=await request.put('/api/coaching/profile',{...auth,data:profile});expect(saved.status()).toBe(200)
  const readback=await request.get('/api/coaching/profile',auth);expect(readback.status()).toBe(200);const stored=await readback.json();expect(stored).toMatchObject({...profile,benchmark:{...profile.benchmark,observed_at:stored.benchmark.observed_at}});expect(new Date(stored.benchmark.observed_at).getTime()).toBe(new Date(profile.benchmark.observed_at).getTime())
  await page.addInitScript(({token,tokenId,user})=>{localStorage.setItem('loopback.token',token);localStorage.setItem('loopback.tokenId',tokenId);localStorage.setItem('loopback.user',JSON.stringify(user))},session)
  const queueResponse=await request.get('/api/queue',auth);expect(queueResponse.status()).toBe(200);const queue=await queueResponse.json()
  await page.goto('/coach');await expect(page.getByLabel('Goal',{exact:true})).toBeEnabled();await page.getByLabel('Goal',{exact:true}).selectOption('general_fitness');await page.getByLabel('Event date',{exact:true}).fill('');await page.getByLabel('Target time (optional)',{exact:true}).fill('');await page.getByLabel('Benchmark source',{exact:true}).fill(`Synthetic default start ${randomUUID()}`);for(const day of ['mon','tue','wed','thu','fri','sat','sun']){const button=page.getByRole('button',{name:day,exact:true});if(await button.getAttribute('aria-pressed')==='true')await button.click()}const today=await page.evaluate(()=>['sun','mon','tue','wed','thu','fri','sat'][new Date().getDay()]);await page.getByRole('button',{name:today,exact:true}).click()
  const pending=page.waitForResponse(r=>r.url().endsWith('/api/coaching/program')&&r.request().method()==='POST');await page.getByRole('button',{name:'Preview program',exact:true}).click();const response=await pending;const body=await response.json();if(response.ok())draftId=body.plan.id;expect(response.status(),JSON.stringify(body.detail)).toBe(201);const dialog=page.getByRole('dialog',{name:'Review coaching proposal',exact:true});await expect(dialog).toBeVisible();await dialog.getByRole('button',{name:'Close without changes',exact:true}).click();expect(await (await request.get('/api/queue',auth)).json()).toEqual(queue)
 }finally{
  try{if(draftId&&auth){const removed=await request.delete(`/api/plans/${draftId}`,auth);expect(removed.status()).toBe(204)}}
  finally{
   try{if(actorId){const deactivated=await request.patch(`/api/admin/users/${actorId}`,{...admin,data:{isActive:false}});expect(deactivated.status()).toBe(200)}}
   finally{const sharedAfter=await request.get('/api/coaching/profile',sharedAuth);expect(sharedAfter.status()).toBe(200);expect(await sharedAfter.json()).toEqual(sharedBefore)}
  }
 }
})
