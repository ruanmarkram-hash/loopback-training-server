import {decimalSeconds} from '../lib/coach-format'
import {useEffect, useRef, useState} from 'react'

/** Elapsed duration is separate from clock time. Decimal seconds remain visible. */
export function ElapsedInput({label, name, value, onChange, pace=false, optional=false}: {
 label:string; name:string; value:string; onChange:(value:string)=>void; pace?:boolean; optional?:boolean
}) {
 const split=(text:string)=>{if(!text)return pace?['','']:['','',''];const parts=text.split(':').map(part=>part===''?'':decimalSeconds(Number(part)));if(pace)return parts;if(parts.length===3)return parts;const minutes=Number(parts[0]);return [String(Math.floor(minutes/60)),String(minutes%60),parts[1]||'0']}
 const [parts,setParts]=useState(()=>split(value))
 const emitted=useRef(value)
 useEffect(()=>{if(value!==emitted.current){setParts(split(value));emitted.current=value}},[value,pace])
 const secondsText=(part:string)=>{const [whole,fraction]=decimalSeconds(Number(part||'0')).split('.');return whole.padStart(2,'0')+(fraction!==undefined?'.'+fraction:'')}
 const change=(next:string[])=>{setParts(next);const text=next.every(part=>part==='')?'':next.map((part,index)=>index===next.length-1?secondsText(part):!pace&&index===1?(part||'0').padStart(2,'0'):part||'0').join(':');emitted.current=text;onChange(text)}
 const units=pace?['Minutes','Seconds']:['Hours','Minutes','Seconds']
 return <fieldset className="elapsed-input"><legend>{label}</legend><div className="elapsed-parts">{units.map((unit,index)=><label key={unit}>{unit}<input className="field-input" type="number" inputMode={unit==='Seconds'?'decimal':'numeric'} name={name} aria-label={`${label} ${unit.toLowerCase()}`} min="0" max={unit==='Seconds'?59.99999999999999:unit==='Minutes'&&!pace?59:undefined} step={unit==='Seconds'?'any':'1'} value={parts[index]??''} onChange={event=>change(parts.map((part,i)=>i===index?event.target.value:part))}/></label>)}</div><span className="coach-muted">{pace?'Elapsed minutes and seconds per kilometre.':'Elapsed duration, not a time of day.'}</span>{optional&&<button className="btn-ghost elapsed-clear" type="button" onClick={()=>change(units.map(()=>''))}>Clear {label.toLowerCase()}</button>}</fieldset>
}
