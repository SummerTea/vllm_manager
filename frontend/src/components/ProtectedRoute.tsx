import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useUser } from '@/contexts/UserContext'

/**
 * 路由守卫：未登录时重定向到 /login，并携带来源地址（state.from）。
 */
export function ProtectedRoute() {
  const { user, loading } = useUser()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-primary" />
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  return <Outlet />
}
