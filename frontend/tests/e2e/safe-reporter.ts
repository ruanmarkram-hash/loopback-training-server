import type { Reporter, TestCase, TestResult, FullResult, FullConfig, Suite, TestError } from '@playwright/test/reporter'
import { readFileSync, mkdirSync, writeFileSync, existsSync } from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
const root=path.resolve('../../../artifacts/web-qa')
function clean(value:unknown):unknown {
 let serialized=JSON.stringify(value)
 const env=readFileSync(path.resolve(process.env.LOOPBACK_QA_FIXTURE==='candidate' ? '../../../artifacts/baseline-server/candidate/private.env' : '../../../artifacts/baseline-server/private.env'),'utf8')
 for(const line of env.split(/\r?\n/)) { const i=line.indexOf('='); if(i<1)continue; const secret=line.slice(i+1).replace(/^['"]|['"]$/g,'');if(secret.length>7)serialized=serialized.split(secret).join('<REDACTED>') }
 const bootstrapFile=path.resolve('../../../artifacts/baseline-server/bootstrap-qa/private.env')
 if(existsSync(bootstrapFile)){const password=readFileSync(bootstrapFile,'utf8').split(/\r?\n/).find(line=>line.startsWith('QA_PASSWORD='))?.slice('QA_PASSWORD='.length).replace(/^['\"]|['\"]$/g,'');if(password)serialized=serialized.split(password).join('<REDACTED>')}
 const evidenceAccountFile=path.resolve('../../../artifacts/baseline-server/isolated-verification/web-evidence.private.json')
 if(existsSync(evidenceAccountFile)){const password=JSON.parse(readFileSync(evidenceAccountFile,'utf8')).password;if(password)serialized=serialized.split(password).join('<REDACTED>')}
 const accountB = JSON.parse(readFileSync(path.join(root,process.env.LOOPBACK_QA_FIXTURE==='candidate' ? 'private-candidate-accounts.json' : 'private-accounts.json'),'utf8'))
 serialized=serialized.split(accountB.password).join('<REDACTED>')
 serialized=serialized.replace(/qa-secret-[a-f0-9-]+/g,'<REDACTED_PASSWORD>')
 serialized=serialized.replace(/tapi_[A-Za-z0-9_-]+/g,'<REDACTED_TOKEN>')
 return JSON.parse(serialized)
}
export default class SafeReporter implements Reporter {
 results:unknown[]=[]
 cases:any[]=[]
 selectedCaseIds:string[]=[]
 configuredRetries=0
 configuredWorkers=0
 harnessErrors:unknown[]=[]
 onBegin(config:FullConfig,suite:Suite){this.configuredRetries=Math.max(0,...config.projects.map(project=>project.retries));this.configuredWorkers=config.workers;this.selectedCaseIds=suite.allTests().map(test=>test.titlePath().join(' > '))}
 onError(error:TestError){this.harnessErrors.push(clean({message:error.message,stack:error.stack}))}
 onTestEnd(test:TestCase,result:TestResult){
  for(const attachment of result.attachments){if(attachment.path&&/\.(md|txt|json|log)$/.test(attachment.path)&&existsSync(attachment.path)){const text=readFileSync(attachment.path,'utf8');writeFileSync(attachment.path,String(clean(text)))}}
  const entry={testId:test.titlePath().join(' > '),project:test.parent.project()?.name,title:test.title,status:result.status,expectedStatus:test.expectedStatus,retry:result.retry,duration:result.duration,errors:result.errors.map(e=>({message:e.message,stack:e.stack})),attachments:result.attachments.filter(a=>a.path).map(a=>({name:a.name,path:path.relative(path.resolve('../../..'),a.path!),sha256:createHash('sha256').update(readFileSync(a.path!)).digest('hex')}))}
  this.results.push(clean(entry));
  this.cases.push({id:entry.testId,result:result.status,attempts:result.retry+1,executed:result.status!=='skipped',complete:result.status!=='interrupted',scenario_ids:test.annotations.filter(a=>a.type==='scenario').map(a=>a.description),controls:test.annotations.filter(a=>a.type==='control').map(a=>JSON.parse(a.description||'{}'))});console.log(`${result.status}: ${test.parent.project()?.name}: ${test.title}`)
  if(result.status==='failed')console.log(JSON.stringify(clean(entry.errors)))
 }
 onEnd(result:FullResult){mkdirSync(root,{recursive:true});writeFileSync(path.join(root,process.env.LOOPBACK_QA_RESULT_NAME||'results.json'),JSON.stringify({started:result.startTime,finished:new Date().toISOString(),status:result.status,result:result.status,executed:this.cases.some(entry=>entry.executed),complete:!['interrupted','timedout'].includes(result.status)&&this.selectedCaseIds.length>0&&this.selectedCaseIds.every(id=>this.cases.some(entry=>entry.id===id&&entry.complete)),selected_case_ids:this.selectedCaseIds,harness_errors:this.harnessErrors,command:process.env.LOOPBACK_QA_COMMAND||process.argv.join(' '),automatic_retries:this.configuredRetries,configured_workers:this.configuredWorkers,environment:{url:process.env.LOOPBACK_QA_ORIGIN||'http://127.0.0.1:18089',evidence_layer:process.env.LOOPBACK_QA_EVIDENCE_LAYER||'web_ui',build:process.env.LOOPBACK_QA_BUILD_ID||null,image:process.env.LOOPBACK_QA_IMAGE_ID||null,fixture:process.env.LOOPBACK_QA_FIXTURE||'baseline'},cases:this.cases,results:this.results},null,2))}
}
