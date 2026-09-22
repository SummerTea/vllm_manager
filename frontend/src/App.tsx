import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { ProtectedRoute } from '@/components/ProtectedRoute'
import { AppLayout } from '@/components/layout/AppLayout'
import { UserProvider } from '@/contexts/UserContext'
import { Toaster } from '@/components/ui/sonner'

const LoginPage = lazy(() => import('@/pages/Login'))
const HomePage = lazy(() => import('@/pages/Home'))
const NotFoundPage = lazy(() => import('@/pages/NotFoundPage'))

function PageLoading() {
  return (
    <div className="flex h-screen items-center justify-center text-muted-foreground">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-primary" />
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter basename="/vllm_manager/frontend/">
        <UserProvider>
          <Suspense fallback={<PageLoading />}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route element={<ProtectedRoute />}>
                <Route element={<AppLayout />}>
                  <Route path="/" element={<HomePage />} />
                  <Route path="*" element={<NotFoundPage />} />
                </Route>
              </Route>
            </Routes>
          </Suspense>
          <Toaster position="top-center" richColors />
        </UserProvider>
      </BrowserRouter>
    </ErrorBoundary>
  )
}
