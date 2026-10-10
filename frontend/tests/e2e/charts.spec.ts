import {test,expect} from '@playwright/test'
import {useFixtureSession,fixtureApi,screenshot} from './helpers'
const charts=['Recovery history','Sleep history','Weight history','Steps history','Nutrition macros','Energy balance','Protein per kilogram']
test('WEB-CHART-001 keyboard chart values match real recorded first and last observations',async({page,request},info)=>{
 test.setTimeout(60_000)
 for(const id of ['UI-010','UI-011'])info.annotations.push({type:'scenario',description:id})
 for(const id of ['recovery','sleep','weight','steps','nutrition-macros','energy-balance','protein-per-kilogram'])info.annotations.push({type:'control',description:JSON.stringify({id:`web.health.chart-${id}`,states:['keyboard','focus','escape']})})
 await useFixtureSession(page,'athlete');await page.goto('/health')
 const start=await page.evaluate(()=>{const d=new Date();d.setDate(d.getDate()-30);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`})
 const response=await request.get(`/api/health/metrics?start_date=${start}`,fixtureApi('athlete'));expect(response.ok()).toBeTruthy();const days=(await response.json()).reverse();expect(days.length).toBeGreaterThan(1)
 const recovery=page.getByRole('group',{name:'Recovery history',exact:true});await expect(recovery).toBeVisible();await recovery.focus();await page.keyboard.press('Home')
 info.annotations.push({type:'control',description:JSON.stringify({id:'web.health.chart-recovery',states:['first','last']})});let tooltip=recovery.getByRole('tooltip');await expect(tooltip).toContainText(`${Math.round(days[0].resting_heart_rate)} bpm`);await expect(tooltip).toContainText(`${Number(days[0].date.slice(8,10))} ${new Date(days[0].date).toLocaleDateString('en-US',{month:'short'}).toUpperCase()}`)
 await page.keyboard.press('End');await expect(tooltip).toContainText(`${Math.round(days.at(-1).resting_heart_rate)} bpm`)
 for(const name of charts){const chart=page.getByRole('group',{name,exact:true});await chart.scrollIntoViewIfNeeded();await chart.focus();await expect(chart).toBeFocused();await page.keyboard.press('Home');await expect(chart.getByRole('tooltip')).toBeVisible();await page.keyboard.press('End');await expect(chart.getByRole('tooltip')).toBeVisible();await page.keyboard.press('Escape');await expect(chart.getByRole('tooltip')).toHaveCount(0)}
 await screenshot(page,info.project.name,'health-keyboard-last-chart')
})
