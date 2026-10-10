import {spawn} from 'node:child_process'
import {readFileSync,writeFileSync,readdirSync} from 'node:fs'
import {createHash} from 'node:crypto'
import path from 'node:path'
const origin=process.env.LOOPBACK_QA_API_ORIGIN || 'http://127.0.0.1:18093'
async function health(){const response=await fetch(`${origin}/api/health`,{signal:AbortSignal.timeout(10000)});if(!response.ok)throw new Error(`Health returned ${response.status}`);return {captured_at:new Date().toISOString(),body:await response.json()}}
function frontendIdentity(){const files=['package.json','package-lock.json','vite.config.ts'];function walk(dir){for(const entry of readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,entry.name);if(entry.isDirectory())walk(file);else files.push(file)}}walk('src');const hash=createHash('sha256');for(const file of files.sort()){hash.update(file);hash.update('\0');hash.update(readFileSync(file));hash.update('\0')}return {captured_at:new Date().toISOString(),source_sha256:hash.digest('hex'),files:files.length,layer:'development frontend source; separate from backend container UI'}}
function harnessIdentity(){const files=['playwright.config.ts','playwright.review.config.ts','playwright.bootstrap.config.ts'];function walk(dir){for(const entry of readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,entry.name);if(entry.isDirectory())walk(file);else if(/\.(ts|tsx|mjs|md)$/.test(file))files.push(file)}}walk('tests');const hash=createHash('sha256');for(const file of files.sort()){hash.update(file);hash.update('\0');hash.update(readFileSync(file));hash.update('\0')}return {captured_at:new Date().toISOString(),source_sha256:hash.digest('hex'),files:files.length}}
const harnessBefore=harnessIdentity()
const frontendBefore=frontendIdentity()
const before=await health()
const command=['playwright','test',...process.argv.slice(2)]
const exit=await new Promise(resolve=>{const child=spawn('npx',command,{stdio:'inherit',env:{...process.env,LOOPBACK_QA_COMMAND:`npx ${command.join(' ')}`}});child.on('exit',code=>resolve(code ?? 1));child.on('error',()=>resolve(1))})
const after=await health()
const frontendAfter=frontendIdentity()
const harnessAfter=harnessIdentity()
const file=path.resolve('../../../artifacts/web-qa',process.env.LOOPBACK_QA_RESULT_NAME || 'results.json')
const report=JSON.parse(readFileSync(file,'utf8'));report.backend_health={origin,before,after,unchanged:JSON.stringify(before.body)===JSON.stringify(after.body)}
report.backend_identity_stable=report.backend_health.unchanged
report.frontend_identity={before:frontendBefore,after:frontendAfter,unchanged:frontendBefore.source_sha256===frontendAfter.source_sha256}
report.harness_identity={before:harnessBefore,after:harnessAfter,unchanged:harnessBefore.source_sha256===harnessAfter.source_sha256}
report.final_verification_status='requires_source_image_and_frontend_identity_reconciliation'
writeFileSync(file,JSON.stringify(report,null,2));process.exitCode=report.backend_health.unchanged && report.frontend_identity.unchanged && report.harness_identity.unchanged ? exit : 1
