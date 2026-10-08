/** Optional notice storage must never interrupt authentication cleanup or adoption. */
let currentNotice:string|null|undefined
export function readLogoutNotice():string|null {
 if(currentNotice!==undefined)return currentNotice
 try{return sessionStorage.getItem('loopback.logoutNotice')}catch{return null}
}
export function clearLogoutNotice(){
 currentNotice=null
 try{sessionStorage.removeItem('loopback.logoutNotice')}catch{/* Best effort; current-page notice is already cleared. */}
}
export function writeLogoutNotice(value:string){
 currentNotice=value
 try{sessionStorage.setItem('loopback.logoutNotice',value)}catch{/* The current-page fallback preserves this warning. */}
}
