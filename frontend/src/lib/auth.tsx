import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, ApiError, authStorage, setUnauthorizedHandler,bindAuthCredential,clearBoundCredential,setSessionChangedHandler,isAuthStorageEvent,captureAuthCommand } from './api'
import type { AuthUser, LoginResponse } from './types'
import {clearLogoutNotice,writeLogoutNotice} from './logout-notice'

interface AuthState {
  user: AuthUser | null
  tokenId: string | null
  login: (username: string, password: string) => Promise<void>
  /** Store an already-minted session (e.g. the first-run setup response). */
  adoptSession: (res: LoginResponse) => void
  logout: () => Promise<void>
  /** Clear local state without calling the API (used on 401). */
  reset: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<AuthUser | null>(() =>
    authStorage.token ? authStorage.getUser<AuthUser>() : null,
  )
  const [tokenId, setTokenId] = useState<string | null>(() => authStorage.tokenId)

  const resetLocal = useCallback(() => {
    void queryClient.cancelQueries()
    queryClient.clear()
    clearBoundCredential()
    setUser(null)
    setTokenId(null)
  }, [queryClient])

  const reset = useCallback(() => {resetLocal();authStorage.clear()},[resetLocal])
  useEffect(() => {
    setUnauthorizedHandler(reset)
    setSessionChangedHandler(resetLocal)
    const changed=(event:StorageEvent)=>{if(isAuthStorageEvent(event))resetLocal()}
    window.addEventListener('storage',changed)
    return()=>{window.removeEventListener('storage',changed);setUnauthorizedHandler(null);setSessionChangedHandler(null)}
  }, [reset,resetLocal])

  const adoptSession = useCallback((res: LoginResponse) => {
    void queryClient.cancelQueries()
    queryClient.clear()
    clearLogoutNotice()
    authStorage.save(res.token, res.tokenId, res.user)
    bindAuthCredential()
    setUser(res.user)
    setTokenId(res.tokenId)
  }, [queryClient])

  const login = useCallback(
    async (username: string, password: string) => {
      const command=captureAuthCommand()
      const res = await api.post<LoginResponse>(
        '/api/auth/login',
        { username, password, deviceName: 'Web dashboard' },
        { auth: false,command },
      )
      command.assertUnchanged()
      adoptSession(res)
    },
    [adoptSession],
  )

  const logout = useCallback(async () => {
    const command=captureAuthCommand()
    const id = authStorage.tokenId
    clearLogoutNotice()
    try {
      if (id) await api.delete(`/api/auth/tokens/${id}`,{command})
    } catch (error) {
      if (command.isCurrent() && !(error instanceof ApiError && [401, 404].includes(error.status))) {
        writeLogoutNotice('Could not confirm server session revocation. You are signed out in this browser. Sign in to manage the old session in Settings.')
      }
    } finally {
      if(command.isCurrent())reset()
    }
  }, [reset])

  const value = useMemo(
    () => ({ user, tokenId, login, adoptSession, logout, reset }),
    [user, tokenId, login, adoptSession, logout, reset],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth outside AuthProvider')
  return ctx
}
