import { Link } from 'react-router-dom'
import { CaretDown } from '@phosphor-icons/react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { ForecastSession,PublicationReview } from '../lib/coaching'
import type { CompositionStep, WorkoutComposition } from '../lib/types'
import { formatPrescriptionValue } from '../lib/coach-format'
import { ErrorNote, Loading } from './ui'

function alertText(value:unknown):string {
 if(!value||typeof value!=='object')return 'Intensity alert unavailable'
 const alert=value as Record<string,unknown>,range=typeof alert.min==='number'&&typeof alert.max==='number'&&Number.isFinite(alert.min)&&Number.isFinite(alert.max)&&alert.min>0&&alert.max>=alert.min?`${alert.min}–${alert.max}`:null
 if(alert.type==='heartRateZone'||alert.type==='powerZone')return typeof alert.zone==='number'&&Number.isInteger(alert.zone)&&alert.zone>0?`${alert.type==='heartRateZone'?'Heart rate':'Power'} zone ${alert.zone}`:'Intensity zone unavailable'
 if(!range)return 'Intensity range unavailable'
 if(alert.type==='speed'){const unit=alert.unit===undefined||alert.unit==='metersPerSecond'?'m/s':alert.unit==='kilometersPerHour'?'km/h':null;if(!unit)return 'Speed alert unit unavailable';const scale=unit==='m/s'?1000:3600;return `Speed ${range} ${unit} · Pace ${formatPrescriptionValue('pace_seconds_per_km',scale/(alert.max as number))}–${formatPrescriptionValue('pace_seconds_per_km',scale/(alert.min as number))}`}
 if(alert.type==='heartRate')return `Heart rate ${range} bpm`
 if(alert.type==='cadence')return `Cadence ${range} steps/min`
 if(alert.type==='power')return `Power ${range} W`
 return 'Intensity alert type unavailable'
}
export function stepText(step: CompositionStep): string {
 const goal=step.goal
 const units:Record<string,string>={kilometers:'km',miles:'mi',yards:'yd',minutes:'min',kilocalories:'kcal'}
 const value=goal?.value,quantity=goal?.type==='open'?'Open duration':typeof value!=='number'||!Number.isFinite(value)||value<=0?'Prescription unavailable':goal?.unit==='meters'?formatPrescriptionValue('distance_meters',value):goal?.unit==='seconds'?formatPrescriptionValue('duration_seconds',value):goal?.unit&&units[goal.unit]?`${value} ${units[goal.unit]}`:'Prescription unit unavailable'
 const alerts=[...(step.alert!=null?[step.alert]:[]),...(Array.isArray(step.alerts)?step.alerts:[])]
 return `${step.purpose || 'Step'} · ${quantity}${alerts.length?' · '+alerts.map(alertText).join(' · '):''}`
}
/** Render every supported prescription form shared by forecast and approval review. */
export function PrescriptionList({composition}: {composition: WorkoutComposition}) {
 const hasSteps=!!composition.singleGoal || !!composition.warmup || !!composition.cooldown || !!composition.blocks?.some(block=>block.steps?.length)
 if(!hasSteps)return <p>Prescription details unavailable.</p>
 return <ul>{composition.warmup && <li>Warmup · {stepText(composition.warmup)}</li>}{composition.singleGoal && <li>{stepText({purpose:'Single goal',goal:composition.singleGoal})}</li>}{composition.blocks?.map((block,index)=><li key={index}>{block.iterations ?? 1} × {block.steps?.length?block.steps.map(stepText).join(' / '):'Prescription steps unavailable.'}</li>)}{composition.cooldown && <li>Cooldown · {stepText(composition.cooldown)}</li>}</ul>
}
export function ForecastList({ sessions }: {sessions: ForecastSession[]}) {
 return <div className="coach-forecast">{sessions.map((session) => <details className="coach-session" key={session.id}>
  <summary><time dateTime={session.date}>{session.date}</time><strong>{session.title}</strong><CaretDown className="coach-expand" aria-hidden="true" size={16}/><span className="coach-session-status">{session.prescription_committed ? `Committed · ${session.status}` : 'Estimated future session'}</span></summary>
  <div className="coach-session-body">
   {session.phase && <p>Phase: {session.phase.replaceAll('_',' ')} · {session.session_type?.replaceAll('_',' ')}</p>}
   {session.effort && <p>{session.effort}</p>}
   {session.dose && <p>{session.dose.distance_meters != null && `${formatPrescriptionValue('distance_meters',session.dose.distance_meters)} · `}{session.dose.estimated_duration_seconds != null && `${formatPrescriptionValue('duration_seconds',session.dose.estimated_duration_seconds)}${session.dose.duration_estimated ? ' estimated' : ''}`}{session.dose.event_excluded_from_training_load && ' · Event excluded from training load'}</p>}
   <PrescriptionList composition={session.composition}/>
   {!!session.constraints?.length && <ul>{session.constraints.map((constraint,index)=><li key={index}>{constraint}</li>)}</ul>}
   {session.benchmark_evidence && <p>Benchmark source: {session.benchmark_evidence.source} · Observed {new Date(session.benchmark_evidence.observed_at).toLocaleString()} · Confidence {session.benchmark_evidence.confidence ?? 'unknown'}</p>}
  </div>
 </details>)}</div>
}
export function ProgramForecast({ id,name }: {id: string;name?:string}) {
 const query = useQuery({queryKey: ['coaching', 'program', id], queryFn: () => api.get<{forecast: ForecastSession[] | null; forecastApproved: boolean;publicationReview?:PublicationReview}>(`/api/coaching/programs/${id}`), retry: false})
 return <section className="prose-card coach-card" aria-label="Full program forecast">
  <h2>{name?`Current program: ${name}`:'Full program forecast'}</h2><p>Future sessions are estimates. Approval commits the near horizon to the queue. Queue status does not prove delivery to your Watch.</p>
  {query.isPending ? <Loading /> : query.error ? <ErrorNote error={query.error} /> : <>
   {query.data.publicationReview?.status==='review_required'?<div role="status" className="coach-change" aria-label="Future issuance paused for review"><h3>Future issuance paused for review</h3><p>{query.data.publicationReview.reason||'The current facts require review before future sessions can be issued.'}</p><p>Already committed sessions and history remain recorded.</p><h4>Supplied restrictions</h4>{query.data.publicationReview.restrictions.length?<ul>{query.data.publicationReview.restrictions.map((fact,index)=><li key={index}>{fact}</li>)}</ul>:<p>No restrictions supplied.</p>}<p>Supplied available days: {query.data.publicationReview.availableDays.length?query.data.publicationReview.availableDays.join(', '):'none supplied'}</p>{name?<a href="#coaching-facts">Review coaching facts and request a review</a>:<Link to="/coach">Review coaching facts and request a review</Link>}</div>:query.data.publicationReview?.status==='compatible'?<p role="status">Current publication review is compatible.</p>:<p role="status">Future issuance review status is unavailable.</p>}
   <button className="btn-ghost" disabled={query.isFetching} onClick={()=>void query.refetch()}>Refresh program forecast</button>
   {query.data.forecast === null ? <p role="status">Forecast unavailable. Review the program facts before requesting a new forecast.</p> : <ForecastList sessions={query.data.forecast} />}
  </>}
 </section>
}
