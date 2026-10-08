import { describeApiDetail } from './api-errors'
/**
 * Thin typed fetch wrapper. Bearer auth, JSON, and the 401 contract:
 * any 401 means the token is dead (revoked/expired) — wipe local auth
 * and land on /login.
 */

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

const TOKEN_KEY = 'loopback.token'
const TOKEN_ID_KEY = 'loopback.tokenId'
const USER_KEY = 'loopback.user'

export const authStorage = {
  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY)
  },
  get tokenId(): string | null {
    return localStorage.getItem(TOKEN_ID_KEY)
  },
  getUser<T>(): T | null {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    try {
      return JSON.parse(raw) as T
    } catch {
      return null
    }
  },
  save(token: string, tokenId: string, user: unknown) {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(TOKEN_ID_KEY, tokenId)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(TOKEN_ID_KEY)
    localStorage.removeItem(USER_KEY)
  },
}

type Credential = {token:string|null;tokenId:string|null;user:string|null}
const storedCredential=():Credential=>({token:authStorage.token,tokenId:authStorage.tokenId,user:localStorage.getItem(USER_KEY)})
let activeCredential=storedCredential()
export function bindAuthCredential(){activeCredential=storedCredential()}
export function clearBoundCredential(){activeCredential={token:null,tokenId:null,user:null}}
let onSessionChanged:(()=>void)|null=null
export function setSessionChangedHandler(fn:(()=>void)|null){onSessionChanged=fn}
export function isAuthStorageEvent(event:StorageEvent){return event.storageArea===localStorage&&(event.key===null||[TOKEN_KEY,TOKEN_ID_KEY,USER_KEY].includes(event.key))}
function credentialMatches(expected:Credential){const stored=storedCredential();return expected.token===stored.token&&expected.tokenId===stored.tokenId&&expected.user===stored.user}
function checkRequestCredential(credential:Credential){
 if(credential!==activeCredential)throw new ApiError(409,'The session changed while this request was pending. Its result was discarded.')
 if(!credentialMatches(credential)){onSessionChanged?.();throw new ApiError(401,'Your session changed in another tab. Sign in again before continuing.')}
}
export type AuthCommand = {assertCurrent:()=>void;assertUnchanged:()=>void;isCurrent:()=>boolean}
export function captureAuthCommand():AuthCommand {
 const credential=activeCredential
 const stored=storedCredential()
 return {
  assertCurrent:()=>checkRequestCredential(credential),
  assertUnchanged:()=>{if(credential!==activeCredential||!credentialMatches(stored))throw new ApiError(409,'The session changed while this request was pending. Its result was discarded.')},
  isCurrent:()=>credential===activeCredential&&credentialMatches(stored),
 }
}
type RequestOptions={auth?:boolean;command?:AuthCommand}
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: (() => void)|null) {
  onUnauthorized = fn
}

type Query = Record<string, string | number | boolean | undefined | null>

function buildUrl(path: string, query?: Query): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== '') params.set(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions & { query?: Query; body?: unknown } = {},
): Promise<T> {
  const { query, body, auth = true, command } = opts
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const credential=activeCredential
  const guard=()=>{if(command){if(auth)command.assertCurrent();else command.assertUnchanged()}else if(auth)checkRequestCredential(credential)}
  guard()
  if(auth&&!credentialMatches(credential)){onSessionChanged?.();throw new ApiError(401,'Your session changed in another tab. Sign in again before continuing.')}
  if(auth&&credential.token)headers['Authorization']=`Bearer ${credential.token}`

  let res:Response
  try {res = await fetch(buildUrl(path, query), {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })} catch(error){guard();throw error}

  guard()

  if (res.status === 401) {
    if (auth) onUnauthorized?.()
    let detail = 'Unauthorized'
    try {
      detail = describeApiDetail((await res.json()).detail, detail)
    } catch {
      /* keep default */
    }
    throw new ApiError(401, detail)
  }

  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const data = await res.json()
      detail = describeApiDetail(data.detail, detail)
    } catch {
      /* keep default */
    }
    throw new ApiError(res.status, detail)
  }

  if (res.status === 204) return undefined as T
  const data=await res.json()
  guard()
  return data as T
}

export const api = {
  get: <T>(path:string,query?:Query,opts?:RequestOptions)=>request<T>('GET',path,{query,...opts}),
  post: <T>(path:string,body?:unknown,opts?:RequestOptions)=>request<T>('POST',path,{body,...opts}),
  put: <T>(path:string,body?:unknown,opts?:RequestOptions)=>request<T>('PUT',path,{body,...opts}),
  patch: <T>(path:string,body?:unknown,opts?:RequestOptions)=>request<T>('PATCH',path,{body,...opts}),
  delete: <T=void>(path:string,opts?:RequestOptions)=>request<T>('DELETE',path,opts),
}
