import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'

export default function NotFoundPage() {
  return (
    <div className="flex h-full min-h-[50vh] flex-col items-center justify-center gap-4">
      <p className="text-5xl font-bold text-muted-foreground">404</p>
      <p className="text-muted-foreground">页面不存在或已被移除</p>
      <Button asChild>
        <Link to="/">返回首页</Link>
      </Button>
    </div>
  )
}
