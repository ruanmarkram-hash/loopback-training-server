import {expect,type APIRequestContext,type Page} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {fixtureApi} from './helpers'
/** Each mutating program/profile journey owns its actor and retires it in finally. */
export async function provisionOwnedActor(request:APIRequestContext,page:Page,label:string){
 const admin=fixtureApi('admin'),username=`qa-owned-${randomUUID().slice(0,8)}`,password=`qa-secret-${randomUUID()}`
 const created=await request.post('/api/admin/users',{...admin,data:{username,password,displayName:label,role:'user'}});expect(created.status()).toBe(201);const actor=await created.json()
 const cleanup=async()=>{const response=await request.patch(`/api/admin/users/${actor.id}`,{...admin,data:{isActive:false}});expect(response.status()).toBe(200)}
 try{const login=await request.post('/api/auth/login',{data:{username,password,deviceName:'Synthetic owned QA journey'}});if(login.status()===429)throw new Error(`Synthetic fixture login rate limited; Retry-After=${login.headers()['retry-after']||'not supplied'}`);expect(login.status()).toBe(200);const session=await login.json();await page.addInitScript(session=>{localStorage.setItem('loopback.token',session.token);localStorage.setItem('loopback.tokenId',session.tokenId);localStorage.setItem('loopback.user',JSON.stringify(session.user))},session);return {auth:{headers:{Authorization:`Bearer ${session.token}`}},actor,cleanup}}
 catch(error){await cleanup();throw error}
}
