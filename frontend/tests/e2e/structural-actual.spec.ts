import {test, expect, type APIRequestContext} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {provisionOwnedActor} from './owned-actor'
import {expectNoOverflow, screenshot} from './helpers'

type Auth = {headers: {Authorization: string}}
const localDate = (offset: number) => {
 const date = new Date(Date.now() + offset * 86400000)
 return new Intl.DateTimeFormat('en-CA', {timeZone: 'Australia/Brisbane'}).format(date)
}
async function read(request: APIRequestContext, auth: Auth, path: string) {
 const response = await request.get(path, auth)
 expect(response.status()).toBe(200)
 return response.json()
}
async function approve(request: APIRequestContext, auth: Auth, proposal: any) {
 const response = await request.post(`/api/coaching/proposals/${proposal.id}/accept`, {
  ...auth, data: {expectedRevision: proposal.base_revision},
 })
 expect(response.status()).toBe(200)
 return response.json()
}
async function program(request: APIRequestContext, auth: Auth) {
 const facts = {
  goal: {type: '10k', race_date: localDate(150)}, available_days: ['mon','wed','sat'],
  timezone: 'Australia/Brisbane', restrictions: [],
  benchmark: {level: 'established', pace_seconds_per_km: 360, weekly_distance_meters: 15000,
   long_run_meters: 6000, source: 'Synthetic structural public API fixture', observed_at: new Date().toISOString()},
 }
 const profile = await request.put('/api/coaching/profile', {...auth, data: facts})
 expect(profile.status()).toBe(200)
 const response = await request.post('/api/coaching/program', {
  ...auth, data: {...facts, start_date: localDate(3), idempotencyKey: randomUUID()},
 })
 expect(response.status()).toBe(201)
 const preview = await response.json()
 await approve(request, auth, preview.proposal)
 return {id: preview.plan.id, facts}
}

// Preparation uses a labelled synthetic worker result, never an inference service.
async function reducedPlan(request: APIRequestContext, auth: Auth) {
 const created = await request.post('/api/plans', {...auth, data: {
  name: `Synthetic forward undo ${randomUUID()}`, activityType: 'running', status: 'active', startDate: localDate(0),
 }})
 expect(created.status()).toBe(201)
 const plan = await created.json(), queues: any[] = []
 for (const days of [3,7]) {
  const response = await request.post('/api/queue', {...auth, data: {
   planId: plan.id, activityType: 'running', title: `Synthetic future run ${days}`,
   scheduledDate: new Date(Date.now() + days * 86400000).toISOString(),
   workoutData: {activityType: 'running', blocks: [{iterations: 1, steps: [{purpose: 'work', goal: {type: 'distance', value: 5000, unit: 'meters'}}]}]},
  }})
  expect(response.status()).toBe(201); queues.push(await response.json())
 }
 const target = (await read(request, auth, `/api/plans/${plan.id}`)).revision
 const minted = await request.post('/api/auth/tokens', {...auth, data: {name: 'Synthetic structural fixture worker', scope: 'coach_worker'}})
 expect(minted.status()).toBe(201)
 const token = await minted.json(), worker = {headers: {Authorization: `Bearer ${token.token}`}}
 try {
  const review = await request.post('/api/coaching/reviews', {...auth, data: {planId: plan.id, idempotencyKey: randomUUID()}})
  expect(review.status()).toBe(201)
  const claim = await request.post('/api/coaching/worker/claim', {...worker, data: {}})
  expect(claim.status()).toBe(200)
  const job = (await claim.json()).job
  expect(job).toBeTruthy()
  const result = await request.post(`/api/coaching/worker/jobs/${job.id}/result`, {...worker, data: {
   leaseToken: job.leaseToken, result: {status: 'ok', output: {status: 'propose',
    reason: 'Synthetic bounded reduction fixture; not CLI inference evidence',
    proposedChanges: queues.map(q => ({workoutId: q.id, field: 'distance_meters', value: 4750, reason: 'Synthetic fixture reduction'})),
   }, usage: {}},
  }})
  expect(result.status()).toBe(200)
  const proposals = await read(request, auth, '/api/coaching/proposals')
  const proposal = proposals.find((p: any) => p.plan_id === plan.id && p.status === 'awaiting_approval')
  expect(proposal).toBeTruthy(); await approve(request, auth, proposal)
 } finally {
  const revoked = await request.delete(`/api/auth/tokens/${token.tokenId}`, auth)
  expect(revoked.status()).toBe(204)
 }
 return {plan, queues, target}
}

test('WEB-STRUCTURAL-ACTUAL001 undo draft validation cancel awaiting recovery approval and protected history persist', async ({page,request}, info) => {
 test.setTimeout(120000)
 info.annotations.push({type: 'evidence-layer', description: 'actual public HTTP and browser; setup uses synthetic worker result, not CLI inference'})
 await new Promise(resolve => setTimeout(resolve, 13000))
 const owned = await provisionOwnedActor(request,page,'Synthetic actual undo QA')
 try {
  const {plan,queues,target} = await reducedPlan(request,owned.auth)
  const completed = await request.patch(`/api/queue/${queues[0].id}/status`, {...owned.auth,data:{status:'completed'}})
  expect(completed.status()).toBe(200)
  const before = await read(request,owned.auth,'/api/queue')
  const revision = (await read(request,owned.auth,`/api/plans/${plan.id}`)).revision
  const history = await read(request,owned.auth,`/api/coaching/plans/${plan.id}/revisions`)
  await page.goto(`/plans/${plan.id}`)
  await expect(page.getByRole('button',{name:'Rebuild remaining forecast',exact:true})).toHaveCount(0)
  await page.getByRole('button',{name:'Preview future undo',exact:true}).click()
  let form = page.getByRole('dialog',{name:'Preview future undo',exact:true})
  await expect(form.getByLabel('Previous plan revision')).toHaveValue('')
  await form.getByLabel('Previous plan revision').fill('0')
  await form.getByRole('button',{name:'Prepare preview',exact:true}).click()
  expect(await form.getByLabel('Previous plan revision').evaluate((e: HTMLInputElement)=>e.validity.rangeUnderflow)).toBe(true)
  await form.getByRole('button',{name:'Cancel without changes',exact:true}).click()
  expect(await read(request,owned.auth,'/api/queue')).toEqual(before)
  const preview = async () => {
   await page.getByRole('button',{name:'Preview future undo',exact:true}).click()
   form = page.getByRole('dialog',{name:'Preview future undo',exact:true})
   await form.getByLabel('Previous plan revision').fill(String(target))
   const pending = page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname===`/api/coaching/plans/${plan.id}/undo-preview`)
   await form.getByRole('button',{name:'Prepare preview',exact:true}).click()
   const response = await pending; expect(response.status()).toBe(201)
   return response.json()
  }
  const first = (await preview()).proposal
  expect(first.type).toBe('future_undo')
  expect(first.structuralChanges.map((c:any)=>c.workoutId)).toEqual([queues[1].id])
  expect(first.excludedWorkoutIds).toContain(queues[0].id)
  let dialog = page.getByRole('dialog',{name:'Review coaching proposal',exact:true})
  await expect(dialog).toBeVisible(); await expect(dialog).toBeFocused()
  await expect(dialog.getByRole('region',{name:'Current prescription',exact:true})).toContainText('4.75')
  await expect(dialog.getByRole('region',{name:'Proposed prescription',exact:true})).toContainText('5')
  await expectNoOverflow(page); await screenshot(page,info.project.name,'actual-undo-before-approval')
  await dialog.getByRole('button',{name:'Close without changes',exact:true}).click()
  expect(await read(request,owned.auth,'/api/queue')).toEqual(before)
  expect((await read(request,owned.auth,`/api/plans/${plan.id}`)).revision).toBe(revision)
  await page.reload()
  const again = (await preview()).proposal; expect(again.id).toBe(first.id)
  dialog = page.getByRole('dialog',{name:'Review coaching proposal',exact:true})
  await page.keyboard.press('Shift+Tab'); await expect(dialog.getByRole('button',{name:'Approve changes',exact:true})).toBeFocused()
  await dialog.getByRole('button',{name:'Approve changes',exact:true}).dblclick()
  await expect(dialog).toHaveCount(0)
  const after = await read(request,owned.auth,'/api/queue')
  expect(after.find((q:any)=>q.id===queues[0].id)).toEqual(before.find((q:any)=>q.id===queues[0].id))
  expect(after.find((q:any)=>q.id===queues[1].id).workout_data.blocks[0].steps[0].goal.value).toBe(5000)
  const committed = await read(request,owned.auth,`/api/plans/${plan.id}`)
  expect(committed.revision).toBe(revision+1)
  await approve(request,owned.auth,first)
  expect(await read(request,owned.auth,'/api/queue')).toEqual(after)
  expect((await read(request,owned.auth,`/api/plans/${plan.id}`)).revision).toBe(committed.revision)
  const histories = await read(request,owned.auth,`/api/coaching/plans/${plan.id}/revisions`)
  for(const prior of history) expect(histories.find((h:any)=>h.revision===prior.revision)).toEqual(prior)
  await page.reload()
  expect(await read(request,owned.auth,'/api/queue')).toEqual(after)
  expect((await read(request,owned.auth,'/api/coaching/proposals')).find((p:any)=>p.id===first.id).status).toBe('applied')
 } finally {await owned.cleanup()}
})

test('WEB-STRUCTURAL-ACTUAL002 rebuild invalid dates cancel and unknown attestations require review without plan mutation', async ({page,request},info)=>{
 test.setTimeout(120000)
 info.annotations.push({type:'evidence-layer',description:'actual public HTTP/browser; unknown athlete attestations; no positive rebuild or inference claim'})
 await new Promise(resolve=>setTimeout(resolve,13000))
 const owned=await provisionOwnedActor(request,page,'Synthetic actual rebuild QA')
 try {
  const {id}=await program(request,owned.auth)
  const before=await read(request,owned.auth,'/api/queue'),plan=await read(request,owned.auth,`/api/plans/${id}`)
  await page.goto(`/plans/${id}`)
  await page.getByRole('button',{name:'Rebuild remaining forecast',exact:true}).click()
  let dialog=page.getByRole('dialog',{name:'Rebuild remaining forecast',exact:true})
  for(const label of ['Interruption start','Interruption end','Recorded coverage start','Recorded coverage end']) await expect(dialog.getByLabel(label)).toHaveValue('')
  await expect(dialog.getByLabel('I confirm this interruption occurred')).not.toBeChecked()
  await expect(dialog.getByLabel('All completed training in this coverage period is recorded')).not.toBeChecked()
  await dialog.getByLabel('Interruption start').fill(localDate(-1)); await dialog.getByLabel('Interruption end').fill(localDate(-3))
  await dialog.getByLabel('Recorded coverage start').fill(localDate(-14)); await dialog.getByLabel('Recorded coverage end').fill(localDate(-1))
  await dialog.getByRole('button',{name:'Prepare preview',exact:true}).click()
  await expect(dialog.getByRole('alert')).toContainText('start on or before')
  await dialog.getByRole('button',{name:'Cancel without changes',exact:true}).click()
  expect(await read(request,owned.auth,'/api/queue')).toEqual(before)
  await page.getByRole('button',{name:'Rebuild remaining forecast',exact:true}).click()
  dialog=page.getByRole('dialog',{name:'Rebuild remaining forecast',exact:true})
  await dialog.getByLabel('Interruption start').fill(localDate(-3));await dialog.getByLabel('Interruption end').fill(localDate(-1))
  await dialog.getByLabel('Recorded coverage start').fill(localDate(-14));await dialog.getByLabel('Recorded coverage end').fill(localDate(-1))
  const pending=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname===`/api/coaching/programs/${id}/rebuild`)
  await dialog.getByRole('button',{name:'Prepare preview',exact:true}).click()
  const response=await pending;expect(response.status()).toBe(201);const result=await response.json()
  expect(result.status).toBe('review_required');expect(result.proposal).toBeNull();expect(result.forecast).toBeNull()
  await expect(dialog.getByRole('region',{name:'Rebuild outcome'})).toContainText('review required')
  await expect(dialog.getByRole('button',{name:'Approve changes',exact:true})).toHaveCount(0)
  await expectNoOverflow(page);await screenshot(page,info.project.name,'actual-rebuild-unknown-review')
  expect(await read(request,owned.auth,'/api/queue')).toEqual(before)
  expect((await read(request,owned.auth,`/api/plans/${id}`)).revision).toBe(plan.revision)
  await page.keyboard.press('Escape');await expect(dialog).toHaveCount(0);await page.reload()
  expect(await read(request,owned.auth,'/api/queue')).toEqual(before)
 } finally {await owned.cleanup()}
})

test('WEB-PUBLICATION-ACTUAL001 changed supplied facts pause issuance and profile restore recovers visible compatibility',async({page,request},info)=>{
 test.setTimeout(120000)
 info.annotations.push({type:'evidence-layer',description:'actual public API publicationReview/UI; no scheduler future-clock or new issuance claim'})
 await new Promise(resolve=>setTimeout(resolve,13000))
 const owned=await provisionOwnedActor(request,page,'Synthetic publication compatibility QA')
 try {
  const {id,facts}=await program(request,owned.auth)
  const before=await read(request,owned.auth,'/api/queue'),plan=await read(request,owned.auth,`/api/plans/${id}`)
  expect((await read(request,owned.auth,`/api/coaching/programs/${id}`)).publicationReview.status).toBe('compatible')
  const restriction='Synthetic restriction: no running until reviewed'
  const changed=await request.put('/api/coaching/profile',{...owned.auth,data:{...facts,available_days:['sun'],restrictions:[restriction]}})
  expect(changed.status()).toBe(200)
  const paused=await read(request,owned.auth,`/api/coaching/programs/${id}`)
  expect(paused.publicationReview).toMatchObject({status:'review_required',restrictions:[restriction],availableDays:['sun']})
  await page.goto(`/plans/${id}`)
  const notice=page.getByRole('status',{name:'Future issuance paused for review'})
  await expect(notice).toContainText(restriction);await expect(notice).toContainText('sun')
  await expect(notice).toContainText('Already committed sessions and history remain recorded.')
  await expectNoOverflow(page);await screenshot(page,info.project.name,'actual-publication-paused')
  await notice.getByRole('link',{name:'Review coaching facts and request a review',exact:true}).click()
  await expect(page).toHaveURL(/\/coach$/)
  await expect(page.getByRole('status',{name:'Future issuance paused for review'})).toContainText(restriction)
  const restored=await request.put('/api/coaching/profile',{...owned.auth,data:facts});expect(restored.status()).toBe(200)
  await page.getByRole('button',{name:'Refresh status',exact:true}).click()
  await page.goto(`/plans/${id}`);await expect(page.getByText('Current publication review is compatible.',{exact:true})).toBeVisible()
  await page.reload();await expect(page.getByText('Current publication review is compatible.',{exact:true})).toBeVisible()
  expect(await read(request,owned.auth,'/api/queue')).toEqual(before)
  expect((await read(request,owned.auth,`/api/plans/${id}`)).revision).toBe(plan.revision)
 } finally {await owned.cleanup()}
})
