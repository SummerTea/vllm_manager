import { useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { Home, LogOut, Moon, Settings, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useUser } from '@/contexts/UserContext'
import { cn } from '@/lib/utils'

const navItems = [
  { to: '/', label: '首页', icon: Home },
  { to: '/settings', label: '设置', icon: Settings },
]

/**
 * 简洁侧栏布局骨架：左侧导航 + 顶栏（主题切换 / 退出登录）+ 内容区 Outlet。
 */
export function AppLayout() {
  const { user, logout } = useUser()
  const navigate = useNavigate()
  const [dark, setDark] = useState(() =>
    typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
  )

  const toggleTheme = () => {
    const next = !dark
    setDark(next)
    document.documentElement.classList.toggle('dark', next)
  }

  const handleLogout = async () => {
    await logout()
    navigate('/login')
  }

  return (
    <div className="flex h-screen bg-background text-foreground">
      {/* 侧栏 */}
      <aside className="flex w-56 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground">
        <div className="flex h-14 items-center px-4 text-sm font-semibold">
          vllm_manager 管理后台
        </div>
        <nav className="flex flex-1 flex-col gap-1 px-2">
          {navItems.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors',
                  'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
                  isActive &&
                    'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* 主区域 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b px-4">
          <span className="text-sm text-muted-foreground">
            {user?.display_name ?? user?.username ?? ''}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              aria-label="切换主题"
              onClick={toggleTheme}
            >
              {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </Button>
            <Button variant="ghost" size="icon" aria-label="退出登录" onClick={handleLogout}>
              <LogOut className="size-4" />
            </Button>
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
