import { request } from '@playwright/test'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
export default async function setup(){
 const root=path.resolve('../../../artifacts/web-qa'), candidate=process.env.LOOPBACK_QA_FIXTURE==='candidate'
 const file=path.join(root,candidate?'private-candidate-sessions.json':'private-sessions.json')
 const accountFile=path.join(root,candidate?'private-candidate-accounts.json':'private-accounts.json')
 const env=Object.fromEntries(readFileSync(path.resolve(candidate?'../../../artifacts/baseline-server/candidate/private.env':'../../../artifacts/baseline-server/private.env'),'utf8').split(/\r?\n/).filter(l=>l&&!l.startsWith('#')).map(l=>{const i=l.indexOf('=');return [l.slice(0,i),l.slice(i+1).replace(/^['"]|['"]$/g,'')]}))
 const api=await request.newContext({baseURL:process.env.LOOPBACK_QA_ORIGIN||'http://127.0.0.1:18089'})
 if(existsSync(file)){
  const cached=JSON.parse(readFileSync(file,'utf8'))
  const responses=await Promise.all(['admin','athlete','athleteB'].map(role=>api.get('/api/auth/me',{headers:{Authorization:`Bearer ${cached[role].token}`}})))
  if(responses.every(response=>response.ok())){await api.dispose();return}
 }
 const b=existsSync(accountFile)?JSON.parse(readFileSync(accountFile,'utf8')):{username:'loopback-web-qa-b',password:`qa-secret-${randomUUID()}`}
 writeFileSync(accountFile,JSON.stringify(b),{mode:0o600})
 const auth=await api.post('/api/auth/login',{data:{username:'loopback-admin',password:env.BOOTSTRAP_ADMIN_PASSWORD,deviceName:'Synthetic web QA fixture'}})
 if(!auth.ok())throw new Error(`Synthetic admin fixture auth failed (${auth.status()})`)
 const sessions:Record<string,unknown>={admin:await auth.json()}
 const admin=sessions.admin as {token:string}
 const users=await api.get('/api/admin/users',{headers:{Authorization:`Bearer ${admin.token}`}})
 if(!users.ok())throw new Error(`Synthetic fixture inventory failed (${users.status()})`)
 if(!(await users.json()).some((user:{username:string})=>user.username===b.username)){
  const create=await api.post('/api/admin/users',{headers:{Authorization:`Bearer ${admin.token}`},data:{username:b.username,password:b.password,displayName:'Synthetic web QA B',role:'user'}})
  if(!create.ok())throw new Error(`Synthetic athlete B creation failed (${create.status()})`)
 }
 for(const [role,username,password]of [['athlete','loopback-fixture',env.ATHLETE_PASSWORD],['athleteB',b.username,b.password]]){
  const res=await api.post('/api/auth/login',{data:{username,password,deviceName:'Synthetic web QA fixture'}})
  if(!res.ok())throw new Error(`Synthetic ${role} fixture auth failed (${res.status()})`)
  sessions[role]=await res.json()
 }
 writeFileSync(file,JSON.stringify(sessions),{mode:0o600});await api.dispose()
}
