import {expect,type Page} from '@playwright/test'
export async function fillElapsed(page:Page,label:string,value:string,pace=false){
 const parts=value?value.split(':'):[]
 const duration=parts.length===2?[String(Math.floor(Number(parts[0])/60)),String(Number(parts[0])%60),parts[1]]:parts
 const values=pace?parts:duration,units=pace?['minutes','seconds']:['hours','minutes','seconds']
 for(let i=0;i<units.length;i++)await page.getByLabel(`${label} ${units[i]}`,{exact:true}).fill(values[i]||'')
}
export async function expectElapsed(page:Page,label:string,value:string,pace=false){
 const parts=value.split(':'),values=pace?parts:parts.length===2?[String(Math.floor(Number(parts[0])/60)),String(Number(parts[0])%60),parts[1]]:parts
 for(const [i,unit]of(pace?['minutes','seconds']:['hours','minutes','seconds']).entries())await expect(page.getByLabel(`${label} ${unit}`,{exact:true})).toHaveValue(String(Number(values[i])))
}
