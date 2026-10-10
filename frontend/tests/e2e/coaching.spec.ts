import {fillElapsed,expectElapsed} from './elapsed-helpers'
import {test as base,expect,type Page} from '@playwright/test'
import {useFixtureSession as retainedFixtureSession,fixtureApi as retainedFixtureApi,screenshot,expectNoOverflow} from './helpers'

import {provisionOwnedActor} from './owned-actor'
let owned:Awaited<ReturnType<typeof provisionOwnedActor>>|null=null
const test=base.extend<{ownedProgramFixture:void}>({ownedProgramFixture:[async({page,request},use,info)=>{
 if(!/WEB-COACH-00[2346]/.test(info.title)){await use();return}
 // Production login policy is five per minute. This is pacing, never a retry.
 await new Promise(resolve=>setTimeout(resolve,13_000))
 owned=await provisionOwnedActor(request,page,'Synthetic program journey')
 try{const response=await request.put('/api/coaching/profile',{...owned.auth,data:{goal:{type:'general_fitness'},available_days:['mon','wed','sat'],timezone:'Australia/Brisbane',restrictions:[],benchmark:{level:'established',pace_seconds_per_km:360,weekly_distance_meters:18000,long_run_meters:7000,source:'Synthetic athlete supplied program setup',observed_at:new Date().toISOString(),confidence:0.7}}});expect(response.status()).toBe(200);await use()}finally{await owned.cleanup();owned=null}
 },{auto:true,timeout:90_000}]})
function fixtureApi(role:Parameters<typeof retainedFixtureApi>[0]){return role==='athleteB'&&owned?owned.auth:retainedFixtureApi(role)}
async function useFixtureSession(page:Page,role:Parameters<typeof retainedFixtureSession>[1]){if(role==='athleteB'&&owned)return;await retainedFixtureSession(page,role)}

test('WEB-COACH-001 unknown current ability blocks preview without mutation',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'ADAPT-002'},{type:'scenario',description:'ADAPT-003'})
 info.annotations.push({type:'control',description:JSON.stringify({id:'web.coach.preview',states:['missing_availability','missing_current_ability','no_mutation']})})
 await useFixtureSession(page,'athleteB');await page.goto('/coach')
 await expect(page.getByRole('alert').filter({hasText:'Something went wrong'})).toHaveCount(0)
 const before = await request.get('/api/plans',fixtureApi('athleteB'));expect(before.ok()).toBeTruthy();const original=await before.json()
 let previews=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/coaching/program'))previews++})
 await expect(page.getByRole('button',{name:'Refresh status',exact:true})).toBeEnabled()
 await page.getByLabel('Goal',{exact:true}).selectOption('10k')
 for(const day of ['mon','tue','wed','thu','fri','sat','sun']){const button=page.getByRole('button',{name:day,exact:true});if(await button.getAttribute('aria-pressed')==='true')await button.click()}
 await fillElapsed(page,'Current pace','',true)
 for(const label of ['Current weekly distance (km)','Current longest run (km)','Benchmark observation time (local)','Benchmark source'])await page.getByLabel(label,{exact:true}).fill('')
 await page.getByRole('button',{name:'Preview program',exact:true}).click()
 await expect(page.getByRole('alert').filter({hasText:'Choose one to five'})).toBeVisible()
 await page.getByRole('button',{name:'mon',exact:true}).click();await expect(page.getByRole('button',{name:'mon',exact:true})).toHaveAttribute('aria-pressed','true')
 await page.getByLabel('Event date',{exact:true}).fill('2027-03-01')
 await fillElapsed(page,'Target time (optional)','40:00')
 await page.getByRole('button',{name:'Preview program',exact:true}).click()
 await expect(page.getByRole('alert').filter({hasText:'Aspirations cannot replace current ability'})).toBeVisible()
 expect(previews).toBe(0)
 const after = await request.get('/api/plans',fixtureApi('athleteB'));expect(await after.json()).toEqual(original)
 await expectNoOverflow(page);await screenshot(page,info.project.name,'coach-essential-validation')
})

test('WEB-COACH-002 profile reload preview repeat cancel dismiss approve persists once',async({page,request},info)=>{
 test.setTimeout(90_000)
 for(const id of ['ADAPT-001','ADAPT-012','ADAPT-013','UI-003','UI-004'])info.annotations.push({type:'scenario',description:id})
 for(const [id,states] of Object.entries({'web.coach.profile-save':['valid','reload'],'web.coach.preview':['valid','repeat'],'web.coach.proposal-close':['no_activation'],'web.coach.proposal-dismiss':['no_activation','persisted'],'web.coach.proposal-approve':['valid','applied_once','reload']}))info.annotations.push({type:'control',description:JSON.stringify({id,states})})
 const auth=fixtureApi('athleteB'), source=`Synthetic athlete supplied current training ${Date.now()}`
 const get=async(path:string)=>{const result=await request.get(path,auth);expect(result.ok()).toBeTruthy();return await result.json()}
 const oldQueue=await get('/api/queue')
 await useFixtureSession(page,'athleteB');await page.goto('/coach')
 await page.getByLabel('Goal',{exact:true}).selectOption('10k')
 const start=new Date();start.setDate(start.getDate()+2);const date=(value:Date)=>`${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`
 const race=new Date(start);race.setDate(race.getDate()+84)
 await page.getByLabel('Program start',{exact:true}).fill(date(start));await page.getByLabel('Event date',{exact:true}).fill(date(race));await fillElapsed(page,'Target time (optional)','50:00')
 // Clear inherited availability so this fixture remains explicit and repeatable.
 for(const day of ['mon','tue','wed','thu','fri','sat','sun']){const button=page.getByRole('button',{name:day,exact:true});if(await button.getAttribute('aria-pressed')==='true')await button.click()}
 for(const day of ['mon','wed','sat'])await page.getByRole('button',{name:day,exact:true}).click()
 await fillElapsed(page,'Current pace','6:00',true);await page.getByLabel('Current weekly distance (km)',{exact:true}).fill('18');await page.getByLabel('Current longest run (km)',{exact:true}).fill('7')
 await page.getByLabel('Benchmark observation time (local)',{exact:true}).fill(`${date(new Date())}T${String(new Date().getHours()).padStart(2,'0')}:${String(new Date().getMinutes()).padStart(2,'0')}`);await page.getByLabel('Benchmark source',{exact:true}).fill(source);await page.getByLabel('Benchmark confidence (optional, 0–1)',{exact:true}).fill('0.7')
 await page.getByRole('button',{name:'Save coaching profile',exact:true}).click();await expect(page.getByRole('status')).toHaveText('Coaching profile saved.')
 const profile=await get('/api/coaching/profile');expect(profile.benchmark).toMatchObject({pace_seconds_per_km:360,weekly_distance_meters:18000,long_run_meters:7000,confidence:0.7,source:source});expect(profile.goal.target_seconds).toBe(3000)
 await page.reload();await expectElapsed(page,'Current pace','6:00',true);await expect(page.getByLabel('Current weekly distance (km)',{exact:true})).toHaveValue('18');await expectElapsed(page,'Target time (optional)','50:00')
 // Program start is an intentional preview input, rather than a stored profile fact.
 await page.getByLabel('Program start',{exact:true}).fill(date(start))
 const preview=async()=>{const pending=page.waitForResponse(r=>r.url().endsWith('/api/coaching/program')&&r.request().method()==='POST');await page.getByRole('button',{name:'Preview program',exact:true}).click();const response=await pending;const data=await response.json();expect(response.status(),JSON.stringify(data.detail)).toBe(201);return data}
 const first=await preview();let dialog=page.getByRole('dialog',{name:'Review coaching proposal',exact:true});await expect(dialog).toBeVisible();await expect(dialog.locator('.coach-session')).toHaveCount(first.plan.forecast.length);expect(first.plan.forecast.length).toBeGreaterThan(12)
 await dialog.locator('.coach-session summary').first().click();await expect(dialog.locator('.coach-session').first()).toHaveAttribute('open','');await expect(dialog.locator('.coach-session').first()).toContainText('warmup')
 await screenshot(page,info.project.name,'coaching-preview-expanded')
 await dialog.getByRole('button',{name:'Close without changes',exact:true}).click();expect(await get('/api/queue')).toEqual(oldQueue);expect((await get(`/api/plans/${first.plan.id}`)).status).toBe('draft')
 const again=await preview();expect(again.plan.id).toBe(first.plan.id);expect(again.proposal.id).toBe(first.proposal.id)
 dialog=page.getByRole('dialog',{name:'Review coaching proposal',exact:true});await dialog.getByRole('button',{name:'Dismiss proposal',exact:true}).click();await expect(dialog).toHaveCount(0);await page.reload();expect((await get('/api/coaching/proposals')).find((p:{id:string})=>p.id===first.proposal.id).status).toBe('dismissed');expect(await get('/api/queue')).toEqual(oldQueue)
 // A distinct goal intent creates a new proposal. No old dismissed draft is silently activated.
 await fillElapsed(page,'Target time (optional)','49:00');await page.getByLabel('Program start',{exact:true}).fill(date(start));const second=await preview();expect(second.plan.id).not.toBe(first.plan.id)
 dialog=page.getByRole('dialog',{name:'Review coaching proposal',exact:true});await expect(dialog.getByRole('button',{name:'Approve changes',exact:true})).toBeEnabled();await dialog.getByRole('button',{name:'Approve changes',exact:true}).dblclick();await expect(dialog).toHaveCount(0)
 const committed=await get(`/api/plans/${second.plan.id}`);expect(committed.status).toBe('active');expect(committed.revision).toBe(second.proposal.base_revision+1)
 const queued=await get('/api/queue');expect(queued.length).toBeGreaterThan(oldQueue.length);const planQueue=queued.filter((item:{plan_id:string})=>item.plan_id===second.plan.id);expect(planQueue.length).toBeGreaterThan(0);expect(planQueue.length).toBeLessThan(second.plan.forecast.length);for(const item of planQueue){expect(new Date(item.scheduled_date).getTime()).toBeLessThanOrEqual(Date.now()+14*86400000);expect(item.workout_data.blocks.length).toBeGreaterThan(0)}
 const repeat=await request.post(`/api/coaching/proposals/${second.proposal.id}/accept`,{...auth,data:{expectedRevision:second.proposal.base_revision}});expect(repeat.ok()).toBeTruthy();expect((await get(`/api/plans/${second.plan.id}`)).revision).toBe(committed.revision);expect(await get('/api/queue')).toEqual(queued)
 await page.reload();await page.goto(`/plans/${second.plan.id}`);await expect(page.getByRole('region',{name:'Full program forecast'})).toBeVisible();await expect(page.locator('.coach-session')).toHaveCount(second.plan.forecast.length);await expect(page.locator('.g-text').first()).toContainText('49:00 finish');await expect(page.locator('.g-text').first()).not.toContainText('target_seconds');await expect(page.locator('.g-text').first()).not.toContainText('race_date');await expect(page.locator('.g-text').first()).not.toContainText(date(race));await screenshot(page,info.project.name,'coaching-approved-full-forecast')
})

test('WEB-COACH-003 unsaved profile edit clears saved status and retains persisted facts',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'UI-012'},{type:'control',description:JSON.stringify({id:'web.coach.profile-save',states:['saved_status','edited_unsaved_status','no_unsaved_mutation']})})
 await useFixtureSession(page,'athleteB');await page.goto('/coach')
 await expect(page.getByRole('button',{name:'Refresh status',exact:true})).toBeEnabled()
 await page.getByLabel('Goal',{exact:true}).selectOption('general_fitness');await fillElapsed(page,'Current pace','6:00',true);await page.getByLabel('Current weekly distance (km)',{exact:true}).fill('18');await page.getByLabel('Current longest run (km)',{exact:true}).fill('7');await page.getByLabel('Benchmark source',{exact:true}).fill('Synthetic saved-status regression');await page.getByLabel('Benchmark observation time (local)',{exact:true}).fill(new Date().toISOString().slice(0,16));for(const day of ['mon','tue','wed','thu','fri','sat','sun']){const button=page.getByRole('button',{name:day,exact:true});if(await button.getAttribute('aria-pressed')==='true')await button.click()};await page.getByRole('button',{name:'mon',exact:true}).click()
 await page.getByRole('button',{name:'Save coaching profile',exact:true}).click();await expect(page.getByRole('status')).toHaveText('Coaching profile saved.')
 await fillElapsed(page,'Current pace','6:01',true)
 await expect(page.getByRole('status')).toHaveCount(0)
 const persisted=await request.get('/api/coaching/profile',fixtureApi('athleteB'));expect((await persisted.json()).benchmark.pace_seconds_per_km).toBe(360)
 await page.reload();await expectElapsed(page,'Current pace','6:00',true)
})

test('WEB-COACH-004 forecast native controls participate in modal keyboard focus',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'UI-010'},{type:'control',description:JSON.stringify({id:'web.coach.forecast-session',states:['keyboard_open','keyboard_collapse','focus_reachable']})})
 await useFixtureSession(page,'athleteB');await page.goto('/coach');await expectElapsed(page,'Current pace','6:00',true)
 await page.getByLabel('Goal',{exact:true}).selectOption('general_fitness');await page.getByLabel('Event date',{exact:true}).fill('');const future=new Date();future.setDate(future.getDate()+2);await page.getByLabel('Program start',{exact:true}).fill(`${future.getFullYear()}-${String(future.getMonth()+1).padStart(2,'0')}-${String(future.getDate()).padStart(2,'0')}`);await page.getByLabel('Benchmark source',{exact:true}).fill(`Synthetic modal keyboard ${Date.now()}`)
 const oldQueue=await request.get('/api/queue',fixtureApi('athleteB'));const queue=await oldQueue.json()
 await page.getByRole('button',{name:'Preview program',exact:true}).click();const dialog=page.getByRole('dialog',{name:'Review coaching proposal',exact:true});await expect(dialog).toBeVisible();await screenshot(page,info.project.name,'coaching-modal-keyboard-initial')
 const first=dialog.locator('.coach-session summary').first();await expect(dialog).toBeFocused();await page.keyboard.press('Shift+Tab');await expect(dialog.getByRole('button',{name:'Approve changes',exact:true})).toBeFocused();await page.keyboard.press('Tab');const firstFocusable=dialog.locator('button:enabled, input:enabled, textarea:enabled, select:enabled, a[href], summary, [tabindex]:not([tabindex="-1"])').filter({visible:true}).first();await expect(firstFocusable).toBeFocused();let tabs=0;while(!(await first.evaluate(element=>element===document.activeElement))&&tabs++<200)await page.keyboard.press('Tab');await expect(first).toBeFocused();await page.keyboard.press('Enter');await expect(dialog.locator('.coach-session').first()).toHaveAttribute('open','');await page.keyboard.press('Enter');await expect(dialog.locator('.coach-session').first()).not.toHaveAttribute('open','')
 await dialog.focus();await page.keyboard.press('Shift+Tab');await expect(dialog.getByRole('button',{name:'Approve changes',exact:true})).toBeFocused();await page.keyboard.press('Tab');await expect(firstFocusable).toBeFocused()
 await page.keyboard.press('Escape');await expect(dialog).toHaveCount(0);const after=await request.get('/api/queue',fixtureApi('athleteB'));expect(await after.json()).toEqual(queue)
})

test('WEB-COACH-005 profile hydration disables edits until stored facts load',async({page},info)=>{
 info.annotations.push({type:'scenario',description:'UI-005'},{type:'control',description:JSON.stringify({id:'web.coach.goal',states:['initial_loading_disabled','stored_facts_loaded']})})
 await useFixtureSession(page,'athleteB')
 let release!:()=>void;const gate=new Promise<void>(resolve=>release=resolve)
 await page.route('**/api/coaching/status',async route=>{await gate;await route.continue()})
 await page.goto('/coach')
 try {await expect(page.getByLabel('Goal',{exact:true})).toBeDisabled()} finally {release()}
 await expect(page.getByLabel('Goal',{exact:true})).toBeEnabled()
 await page.getByLabel('Goal',{exact:true}).selectOption('10k');await expect(page.getByLabel('Goal',{exact:true})).toHaveValue('10k')
})

test('WEB-COACH-006 initial status failure preserves existing restrictions through recovery and save',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'UI-007'},{type:'scenario',description:'ADAPT-001'})
 info.annotations.push({type:'control',description:JSON.stringify({id:'web.coach.profile-save',states:['initial_failure_disabled','stored_restrictions_preserved','reload']})})
 const auth=fixtureApi('athleteB'), now=new Date(),pad=(n:number)=>String(n).padStart(2,'0'),observed=`${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`
 const fixture={goal:{type:'general_fitness'},available_days:['mon','wed','sat'],timezone:'Australia/Brisbane',units:'metric',restrictions:['Synthetic current knee restriction'],benchmark:{level:'established',pace_seconds_per_km:360,weekly_distance_meters:18000,long_run_meters:7000,source:'Synthetic reviewed profile source',observed_at:now.toISOString(),confidence:0.7}}
 const seeded=await request.put('/api/coaching/profile',{...auth,data:fixture});expect(seeded.ok()).toBeTruthy()
 await useFixtureSession(page,'athleteB');let calls=0
 await page.route('**/api/coaching/status',route=>++calls===1?route.fulfill({status:503,json:{detail:'Synthetic initial coaching status failure'}}):route.continue())
 let writes=0;page.on('request',r=>{if(r.method()==='PUT'&&r.url().endsWith('/api/coaching/profile'))writes++})
 await page.goto('/coach');await expect(page.getByRole('alert')).toContainText('Synthetic initial coaching status failure');await expect(page.getByLabel('Goal',{exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'Save coaching profile',exact:true})).toBeDisabled();expect(writes).toBe(0)
 const unchanged=await request.get('/api/coaching/profile',auth);expect((await unchanged.json()).restrictions).toEqual(fixture.restrictions)
 await page.getByRole('button',{name:'Refresh status',exact:true}).click();await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue(fixture.benchmark.source)
 await page.getByLabel('Benchmark source',{exact:true}).fill('Synthetic dirty facts preserved');await page.getByRole('button',{name:'Refresh status',exact:true}).click();await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue('Synthetic dirty facts preserved')
 await page.getByLabel('Benchmark observation time (local)',{exact:true}).fill(observed);await page.getByRole('button',{name:'Save coaching profile',exact:true}).click();await expect(page.getByRole('status')).toHaveText('Coaching profile saved.')
 const saved=await request.get('/api/coaching/profile',auth);expect((await saved.json()).restrictions).toEqual(fixture.restrictions);expect(writes).toBe(1);await page.reload();await expect(page.getByLabel('Benchmark source',{exact:true})).toHaveValue('Synthetic dirty facts preserved')
})
