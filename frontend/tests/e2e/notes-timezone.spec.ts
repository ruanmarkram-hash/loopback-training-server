import {test,expect} from '@playwright/test'
import {useFixtureSession,fixtureApi} from './helpers'
test.use({timezoneId:'America/New_York'})
test('WEB-NOTES-002 expiry local date survives reopen and save across UTC year boundary',async({page,request},info)=>{
 info.annotations.push({type:'scenario',description:'UI-006'},{type:'control',description:JSON.stringify({id:'web.notes.edit-expiry',states:['year-boundary']})})
 const auth=fixtureApi('athleteB'),summary=`Synthetic local expiry ${Date.now()}`,expiry='2027-01-02T04:59:59Z';const result=await request.post('/api/plan-notes',{...auth,data:{kind:'observation',summary,importance:1,expiresAt:expiry}});expect(result.status()).toBe(201);const note=await result.json()
 try{await useFixtureSession(page,'athleteB');await page.goto('/notes');await page.locator('.note-card').filter({hasText:summary}).getByTitle('Edit',{exact:true}).click();await expect(page.getByLabel('Expires (optional)',{exact:true})).toHaveValue('2027-01-01');await page.getByRole('button',{name:'Save',exact:true}).click();await page.reload();await page.locator('.note-card').filter({hasText:summary}).getByTitle('Edit',{exact:true}).click();await expect(page.getByLabel('Expires (optional)',{exact:true})).toHaveValue('2027-01-01');const rows=await request.get('/api/plan-notes',auth);const saved=(await rows.json()).find((n:{id:string})=>n.id===note.id);expect(new Date(saved.expiresAt).getTime()).toBe(new Date(expiry).getTime())}finally{await request.delete(`/api/plan-notes/${note.id}`,auth)}
})
