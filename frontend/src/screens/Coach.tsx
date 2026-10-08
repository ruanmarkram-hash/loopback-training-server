import {ProgramForecast} from '../components/ProgramForecast'
import {usePlans} from '../lib/queries'
import {captureAuthCommand} from '../lib/api'
import type {AuthCommand} from '../lib/api'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { usePageHeader } from '../components/PageHeader'
import { ErrorNote, Loading } from '../components/ui'
import { ProposalReview } from '../components/ProposalReview'
import { api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { preparePreviewIntent, recordPreviewProposal, terminalIntentHashes } from '../lib/preview-intent'
import type { PreviewIntent } from '../lib/preview-intent'
import { coaching } from '../lib/coaching'
import type { CoachProfile, Proposal } from '../lib/coaching'
import { formatPrescriptionValue, parseGoalTime, parsePace } from '../lib/coach-format'
import { addDays, toDateKey, todayKey } from '../lib/format'
import '../styles/coach.css'

function localTimestamp(date: Date) {const pad = (value: number) => String(value).padStart(2,'0');return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`}
function formatTime(seconds: number) {const value = Math.round(seconds);return `${Math.floor(value/60)}:${String(value%60).padStart(2,'0')}`}
const DAYS = ['mon','tue','wed','thu','fri','sat','sun']
const GOALS = [['first_5k','First 5 km'],['5k','5 km'],['10k','10 km'],['half_marathon','Half marathon'],['marathon','Marathon'],['general_fitness','General fitness']]
export function Coach() {
 const {user}=useAuth()
 const currentPlans=usePlans('active')
 usePageHeader('Coaching','Current ability, full program and explicit approvals')
 const client = useQueryClient()
 const mounted = useRef(true)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const assertScreenCurrent=()=>{if(!mounted.current || window.location.pathname.replace(/\/+$/,'')!=='/coach')throw new Error("The coaching screen changed. Open a fresh preview from current facts.")}
 const status = useQuery({queryKey: ['coaching','status'], queryFn: coaching.status, retry: false, refetchInterval: 15_000})
 const refresh = async () => {if(!mounted.current || window.location.pathname.replace(/\/+$/,'')!=='/coach')return;await Promise.all(['coaching','plans','plan','queue','calendar','plan-schedule'].map(key => client.invalidateQueries({queryKey: [key]})))}
 const [goal,setGoal] = useState(''), [race,setRace] = useState(''), [start,setStart] = useState(()=>toDateKey(addDays(new Date(),2))), [days,setDays] = useState<string[]>([])
 const [pace,setPace] = useState(''), [weekly,setWeekly] = useState(''), [longRun,setLongRun] = useState(''), [observed,setObserved] = useState(''), [target,setTarget] = useState(''), [confidence,setConfidence] = useState(''), [beginner,setBeginner] = useState(false)
 const [timezone,setTimezone] = useState('Australia/Brisbane'), [source,setSource] = useState(''), [validation,setValidation] = useState(''), [selected,setSelected] = useState<Proposal|null>(null), [preparing,setPreparing] = useState(false)
 const preparingRef = useRef(false)
 const previewIntent = useRef<PreviewIntent|null>(null)
 const hydrated = useRef(false)
 const originalProfile = useRef<CoachProfile|null>(null)
 const editedFacts = useRef(new Set<string>())
 useEffect(() => {
  if (hydrated.current || !status.isSuccess) return
  hydrated.current = true
  const profile = status.data?.profile
  originalProfile.current = profile || null
  if (!profile) return
  if (profile.goal) {setGoal(profile.goal.type);setRace(profile.goal.race_date || '');setTarget(profile.goal.target_seconds ? formatTime(profile.goal.target_seconds) : '')}
  setDays(profile.available_days || []);setTimezone(profile.timezone || 'Australia/Brisbane')
  const benchmark = profile.benchmark
  if (benchmark) {setBeginner(benchmark.level === 'beginner');setPace(benchmark.pace_seconds_per_km ? formatTime(benchmark.pace_seconds_per_km) : '');setWeekly(benchmark.weekly_distance_meters != null ? String(benchmark.weekly_distance_meters/1000) : '');setLongRun(benchmark.long_run_meters != null ? String(benchmark.long_run_meters/1000) : '');setSource(benchmark.source || '');setObserved(benchmark.observed_at ? localTimestamp(new Date(benchmark.observed_at)) : '');setConfidence(benchmark.confidence != null ? String(benchmark.confidence) : '')}
 }, [status.data,status.isSuccess])
 const profileSave = useMutation({mutationFn: (body: unknown) => {assertScreenCurrent();return api.put('/api/coaching/profile', body)}, onSuccess: refresh})
 const program = useMutation({mutationFn: (input:{body:unknown;command:AuthCommand})=>{assertScreenCurrent();return coaching.program(input.body,input.command)}, onSuccess: async data => {if(!mounted.current || window.location.pathname.replace(/\/+$/,'')!=='/coach')return;if(previewIntent.current){recordPreviewProposal(previewIntent.current,data.proposal.id);previewIntent.current=null}await refresh(); if(!mounted.current || window.location.pathname.replace(/\/+$/,'')!=='/coach')return;setSelected({...data.proposal,previewJobId:data.previewJobId||data.proposal.previewJobId})}})
 const review = useMutation({mutationFn: ()=>{assertScreenCurrent();return coaching.review()}, onSuccess: refresh})
 const job = useMutation({mutationFn: ({id,action}: {id: string; action:'retry'|'cancel'}) => {assertScreenCurrent();return coaching.job(id,action)}, onSuccess: refresh})
 async function preview(event: React.FormEvent, saveProfile = false) {
  event.preventDefault(); if (!mounted.current || window.location.pathname.replace(/\/+$/,'')!=='/coach' || !status.isSuccess) return; if (preparingRef.current || program.isPending || profileSave.isPending) return; setValidation('')
  const currentPace = parsePace(pace), targetSeconds = target.trim() ? parseGoalTime(target) : null
  const isBeginner = goal === 'first_5k' && beginner
  if (!saveProfile && (!days.length || days.length > 5)) {setValidation('Choose one to five training days.');return}
  if (!['general_fitness','first_5k'].includes(goal) && !race) {setValidation('Enter your event date.');return}
  if (target.trim() && !targetSeconds) {setValidation('Target time must be m:ss or h:mm:ss.');return}
  if ((!isBeginner && !currentPace) || !weekly.trim() || !longRun.trim() || (isBeginner ? Number(weekly)<0 || Number(longRun)<0 : !(Number(weekly)>0) || !(Number(longRun)>0)) || !observed || !source.trim()) {setValidation(isBeginner ? 'Supply current weekly distance and longest run, observation time and source. Enter zero only if it reflects your current running.' : 'Supply current pace, weekly distance, longest run, observation time and source. Aspirations cannot replace current ability.');return}
  const parsedBenchmark = {...(isBeginner ? {level:'beginner'} : {level:'established',pace_seconds_per_km:currentPace}),weekly_distance_meters:Number(weekly)*1000,long_run_meters:Number(longRun)*1000,source:source.trim(),observed_at:new Date(observed).toISOString(),...(confidence.trim() ? {confidence:Number(confidence)} : {})}
  const benchmark = {...originalProfile.current?.benchmark,...parsedBenchmark}
  for (const key of Object.keys(originalProfile.current?.benchmark || {}) as (keyof NonNullable<CoachProfile['benchmark']>)[]) {if (!editedFacts.current.has(key)) Object.assign(benchmark,{[key]:originalProfile.current!.benchmark![key]})}
  if(editedFacts.current.has('confidence')&&!confidence.trim())delete benchmark.confidence
  if(isBeginner&&editedFacts.current.has('level'))delete benchmark.pace_seconds_per_km
  const body = {goal:{type:goal,...(race ? {race_date:race}:{}),...(targetSeconds ? {target_seconds:targetSeconds}:{})}, available_days:days, benchmark, start_date:start, timezone}
  if (originalProfile.current?.goal && !editedFacts.current.has('target_seconds')) {if (originalProfile.current.goal.target_seconds != null) body.goal.target_seconds=originalProfile.current.goal.target_seconds}
  if (saveProfile) profileSave.mutate({goal:body.goal,available_days:days,benchmark,timezone,units:'metric',restrictions:status.data?.profile?.restrictions || []})
  else {
   preparingRef.current=true;setPreparing(true)
   try {const command=captureAuthCommand();const terminalHashes=terminalIntentHashes(status.data.proposals,user?.id||'','initial_program');previewIntent.current=await preparePreviewIntent(user?.id||'','initial','new',body,terminalHashes);assertScreenCurrent();command.assertCurrent();program.mutate({body:{...body,idempotencyKey:previewIntent.current.key},command})}
   catch(error) {if(mounted.current && window.location.pathname.replace(/\/+$/,'')==='/coach')setValidation(error instanceof Error?error.message:'Could not prepare a secure preview request. Try again.')}
   finally {preparingRef.current=false;if(mounted.current && window.location.pathname.replace(/\/+$/,'')==='/coach')setPreparing(false)}
  }
 }
 return <div className="screen coach-screen">
  <section id="coaching-facts" className="prose-card coach-card"><h2>Build your running program</h2><p>Describe your current ability separately from your goal. Saving current facts queues a review. Previewing creates a draft. Approval activates the plan and queues only the near horizon.</p>
   <form onSubmit={preview} onChange={event=>{const name=event.target instanceof HTMLElement?event.target.getAttribute('name'):null;if(name)editedFacts.current.add(name);profileSave.reset();program.reset();setValidation('')}}><fieldset className="coach-form" disabled={!status.isSuccess || preparing || program.isPending || profileSave.isPending}>
    <label>Goal<select aria-label="Goal" className="field-input" required value={goal} onChange={e => {setGoal(e.target.value);if(beginner)editedFacts.current.add('level');setBeginner(false)}}><option value="">Choose a goal</option>{GOALS.map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label>Event date<input aria-label="Event date" data-empty={!race || undefined} aria-describedby={!race?'coach-event-hint':undefined} className="field-input" type="date" value={race} onChange={e=>setRace(e.target.value)} />{!race && <span id="coach-event-hint" className="coach-muted">No event date selected.</span>}</label>
    <label>Program start<input className="field-input" type="date" required min={todayKey()} value={start} onChange={e=>setStart(e.target.value)} /></label>
    <label>Target time (optional)<input name="target_seconds" className="field-input" placeholder="50:00" value={target} onChange={e=>setTarget(e.target.value)} /></label>
    <fieldset className="coach-wide"><legend>Available training days</legend><div className="coach-days">{DAYS.map(day => <button type="button" className={`filter-chip${days.includes(day)?' on':''}`} aria-pressed={days.includes(day)} key={day} onClick={()=>{profileSave.reset();program.reset();setValidation('');setDays(previous=>previous.includes(day)?previous.filter(d=>d!==day):[...previous,day])}}>{day}</button>)}</div></fieldset>
    {goal==='first_5k' && <label className="coach-wide"><input name="level" type="checkbox" checked={beginner} onChange={e=>setBeginner(e.target.checked)} /> I am a beginner and want a conservative walk/run start</label>}
    {!beginner && <label>Current pace (m:ss /km)<input name="pace_seconds_per_km" className="field-input" value={pace} placeholder="6:00" onChange={e=>setPace(e.target.value)} /></label>}<label>Current weekly distance (km)<input name="weekly_distance_meters" className="field-input" type="number" min="0" step="any" value={weekly} onChange={e=>setWeekly(e.target.value)} /></label><label>Current longest run (km)<input name="long_run_meters" className="field-input" type="number" min="0" step="any" value={longRun} onChange={e=>setLongRun(e.target.value)} /></label><label>Benchmark observation time (local)<input name="observed_at" className="field-input" type="datetime-local" value={observed} onChange={e=>setObserved(e.target.value)} /></label><label className="coach-wide">Benchmark source<input name="source" className="field-input" maxLength={200} value={source} placeholder="Recent run or athlete supplied history" onChange={e=>setSource(e.target.value)} /></label><label>Benchmark confidence (optional, 0–1)<input name="confidence" className="field-input" type="number" min="0" max="1" step="any" value={confidence} onChange={e=>setConfidence(e.target.value)} /></label>
    <label className="coach-wide">Timezone<input className="field-input" required value={timezone} onChange={e=>setTimezone(e.target.value)} /></label>
    {validation && <p className="coach-wide" role="alert">{validation}</p>}<div className="coach-wide">{(program.error||profileSave.error) && <ErrorNote error={program.error||profileSave.error}/>}{profileSave.isSuccess && <p role="status">Coaching profile saved.</p>}<div className="coach-actions"><button type="button" className="btn-ghost" disabled={preparing || profileSave.isPending || program.isPending} onClick={event=>preview(event,true)}>Save coaching profile</button><button className="btn-accent" disabled={preparing || program.isPending || profileSave.isPending} type="submit">{program.isPending?'Preparing draft…':'Preview program'}</button></div></div>
   </fieldset></form>
  </section>
  <section className="prose-card coach-card"><h2>Review current training</h2><p>Refresh reads existing status. A review requests a separate coaching job.</p><div className="coach-actions"><button className="btn-ghost" disabled={status.isFetching} onClick={()=>void status.refetch()}>Refresh status</button><button className="btn-accent" disabled={review.isPending} onClick={()=>review.mutate()}>Request review</button></div>{(review.error||job.error||status.error) && <ErrorNote error={review.error||job.error||status.error}/>}
   {status.isPending ? <Loading/> : status.data && <><p>{status.data.coverage.history === 'unknown' ? 'Completed training history is unknown.' : `${status.data.metrics.session_count} completed sessions · ${formatPrescriptionValue('distance_meters',status.data.metrics.actual_distance_meters)} actual distance`} · {status.data.metrics.planned_future_session_count} planned sessions</p>
    <h3>Review jobs</h3>{status.data.jobs.length === 0 && <p>No review jobs yet.</p>}{status.data.jobs.map(item => <div className="coach-change" data-testid={`coaching-job-${item.id}`} key={item.id}><strong>{item.status.replaceAll('_',' ')}</strong><p>{item.data.failureReason||item.data.reason||item.data.failure||item.data.inference?.status||item.data.trigger||'Training review'} · Attempts {item.attempts}</p>{item.status==='dead_letter'&&<button className="btn-ghost" disabled={job.isPending} onClick={()=>job.mutate({id:item.id,action:'retry'})}>Retry review</button>}{['pending','processing'].includes(item.status)&&<button className="btn-ghost" disabled={job.isPending} onClick={()=>job.mutate({id:item.id,action:'cancel'})}>Cancel review</button>}</div>)}
    <h3>Proposals</h3>{status.data.proposals.length===0&&<p>No proposals yet.</p>}{status.data.proposals.map(item=><div className="coach-change" data-testid={`coaching-proposal-${item.id}`} key={item.id}><strong>{item.status.replaceAll('_',' ')}</strong><p>{item.reason}</p><div className="coach-actions"><button className="btn-ghost" onClick={()=>setSelected(item)}>Review proposal</button><Link to={`/plans/${item.plan_id}`}>View plan</Link></div></div>)}</>}
  </section>{currentPlans.error&&<ErrorNote error={currentPlans.error}/>} {currentPlans.data?.filter(plan=>Array.isArray(plan.metadata?.forecast)).map(plan=><ProgramForecast key={plan.id} id={plan.id} name={plan.name}/>)}{selected&&<ProposalReview proposal={selected} onClose={()=>setSelected(null)} refresh={refresh}/>}
 </div>
}
