import {test,expect} from '@playwright/test'
import {useFixtureSession} from './helpers'
test('WEB-HEALTH001 range controls query actual date bounds and show canonical counts; reload returns default',async({page},info)=>{
 info.annotations.push({type:'control',description:JSON.stringify({id:'web.health.date-range',states:['30d','90d','1y']})})
 await useFixtureSession(page,'athlete')
 const start=async(days:number)=>page.evaluate(days=>{const date=new Date();date.setDate(date.getDate()-days);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`},days)
 const expected30=await start(30),first=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/health/metrics'&&new URL(response.url()).searchParams.get('start_date')===expected30)
 await page.goto('/health');const initial=await first;expect(initial.status()).toBe(200);const initialRows=await initial.json();await expect(page.locator('.page-subtitle')).toContainText(`${initialRows.length} days of metrics in range`)
 for(const [label,days]of [['90d',90],['1y',365]] as const){const expected=await start(days),next=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/health/metrics'&&new URL(response.url()).searchParams.get('start_date')===expected);const button=page.getByRole('button',{name:label,exact:true});await button.focus();await page.keyboard.press('Enter');const response=await next;expect(response.status()).toBe(200);const rows=await response.json();await expect(button).toHaveClass('on');await expect(page.locator('.page-subtitle')).toContainText(`${rows.length} days of metrics in range`);expect(rows.length).toBeGreaterThanOrEqual(initialRows.length)}
 await page.getByRole('button',{name:'30d',exact:true}).click();await expect(page.getByRole('button',{name:'30d',exact:true})).toHaveClass('on');await expect(page.locator('.page-subtitle')).toContainText(`${initialRows.length} days of metrics in range`);await page.reload();await expect(page.getByRole('button',{name:'30d',exact:true})).toHaveClass('on');await expect(page.locator('.page-subtitle')).toContainText(`${initialRows.length} days of metrics in range`)
})
