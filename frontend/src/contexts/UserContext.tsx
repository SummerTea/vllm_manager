import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { apiGet, apiPost } from '@/lib/api/client'

export interface User {
  id: string
  username: string
  display_name?: string
}

interface UserContextValue {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
}

const UserContext = createContext<UserContextValue | null>(null)

/**
 * 用户上下文骨架。
 *
 * 约定后端接口：
 *   POST /vllm_manager/web_api/account/login    { username, password } -> data: User
 *   POST /vllm_manager/web_api/account/logout
 *   GET  /vllm_manager/web_api/account/profile  -> data: User
 *
 * 若样板后端尚未实现 account 接口，可临时改为：login 成功后直接
 * setUser({ id: '1', username })（跳过 refreshUser），并把挂载时的
 * refreshUser 调用去掉，前端即可脱离后端独立运行。
 */
export function UserProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [user, setUser] = useState<User | null>(null)
  // 初始为 true：挂载即处于"用户信息加载中"状态，
  // 避免 effect 内同步 setState（react-hooks/set-state-in-effect）
  const [loading, setLoading] = useState(true)

  const refreshUser = useCallback(async () => {
    try {
      const res = await apiGet<{ user?: User }>('/vllm_manager/web_api/account/profile')
      setUser(res.data?.user ?? null)
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshUser()
  }, [refreshUser])

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await apiPost<{ user?: User }>('/vllm_manager/web_api/account/login', {
        username,
        password,
      })
      if (res.data?.user) {
        setUser(res.data.user)
      } else {
        await refreshUser()
      }
      navigate('/')
    },
    [navigate, refreshUser]
  )

  const logout = useCallback(async () => {
    try {
      await apiPost('/vllm_manager/web_api/account/logout')
    } finally {
      setUser(null)
    }
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, logout, refreshUser }),
    [user, loading, login, logout, refreshUser]
  )

  return <UserContext.Provider value={value}>{children}</UserContext.Provider>
}

export function useUser(): UserContextValue {
  const ctx = useContext(UserContext)
  if (!ctx) {
    throw new Error('useUser 必须在 <UserProvider> 内使用')
  }
  return ctx
}
