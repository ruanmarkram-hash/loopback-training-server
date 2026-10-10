import {test,expect} from '@playwright/test'
for(const shape of ['null','empty','populated'] as const)test(`program forecast ${shape} preserves owned plan route`,async({page},info)=>{
 const errors:string[]=[],calls:string[]=[];let writes=0
 page.on('pageerror',e=>errors.push(e.message))
 await page.addInitScript(()=>{localStorage.setItem('loopback.token','synthetic-mock');localStorage.setItem('loopback.tokenId','mock-id');localStorage.setItem('loopback.user',JSON.stringify({id:'synthetic',username:'synthetic',displayName:'Synthetic',role:'user'}))})
 await page.route('**/api/**',route=>{const req=route.request(),path=new URL(req.url()).pathname;if(req.method()!=='GET'){writes++;return route.fulfill({status:500,json:{detail:'Unexpected mutation'}})}let body:unknown=[]
 if(path==='/api/auth/setup')body={required:false}
 if(path==='/api/plans/synthetic-plan')body={id:'synthetic-plan',name:'Synthetic cache A',activity_type:'running',status:'draft',start_date:'2026-11-01',end_date:'2026-12-01',metadata:{forecast:[]},revision:1}
 if(path.endsWith('/schedule'))body={schedule:null,sessions:[]}
 if(path==='/api/coaching/status')body={profile:{},jobs:[],proposals:[],metrics:{},coverage:{history:'unknown'}}
 if(path==='/api/coaching/programs/synthetic-plan'){body={id:'synthetic-plan',revision:1,status:'draft',forecast:shape==='null'?null:shape==='empty'?[]:[{id:'synthetic-session',date:'2026-11-02',title:'Synthetic easy run',status:'forecast',composition:{singleGoal:{type:'distance',value:5000,unit:'meters'}},constraints:['Keep the supplied easy effort.']}],forecastApproved:false,publicationReview:{status:'review_required',reason:'Current program facts require review.',restrictions:[],availableDays:[]}};calls.push(`GET program 200 forecast:${shape}`)}
 return route.fulfill({status:200,json:body})})
 try{
  await page.goto('/plans/synthetic-plan')
  const forecast=page.getByRole('region',{name:'Full program forecast'})
  const review=forecast.getByRole('status',{name:'Future issuance paused for review'})
  await expect(review).toContainText('Current program facts require review.')
  await expect(review).toContainText('Already committed sessions and history remain recorded.')
  await expect(page.locator('.page-title')).toHaveText('Synthetic cache A')
  await expect(page.getByRole('button',{name:'Rebuild remaining forecast',exact:true})).toBeVisible()
  if(shape==='null')await expect(forecast).toContainText('Forecast unavailable')
  else {
   await expect(forecast).not.toContainText('Forecast unavailable')
   await expect(forecast.locator('details.coach-session')).toHaveCount(shape==='empty'?0:1)
   if(shape==='populated'){
    const session=forecast.locator('details.coach-session');await expect(session.locator('summary')).toContainText('Synthetic easy run');await expect(session.locator('time')).toHaveText('2026-11-02')
    await session.locator('summary').click();await expect(session).toContainText('Single goal · 5 km');await expect(session).toContainText('Keep the supplied easy effort.')
   }
  }
  const initialCalls=calls.length,refresh=forecast.getByRole('button',{name:'Refresh program forecast',exact:true})
  await expect(refresh).toBeEnabled();await refresh.click();await expect.poll(()=>calls.length).toBeGreaterThan(initialCalls)
  await expect(refresh).toBeEnabled();await expect(review).toContainText('Current program facts require review.')
  await expect(page.locator('.page-title')).toHaveText('Synthetic cache A');expect(errors).toEqual([]);expect(writes).toBe(0)
 }finally{await info.attach('sanitized-causal-observation',{body:JSON.stringify({shape,errors,calls,writes}),contentType:'application/json'})}
})
