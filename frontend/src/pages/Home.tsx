import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useUser } from '@/contexts/UserContext'

export default function HomePage() {
  const { user } = useUser()

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>欢迎使用 {import.meta.env.VITE_APP_NAME || 'vllm_manager'} 管理后台</CardTitle>
          <CardDescription>
            你好，{user?.display_name ?? user?.username}！
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            这是前后端分离样板工程的前端骨架。页面已挂载在受保护路由下，
            表明登录态校验、侧栏布局与路由已打通。
          </p>
          <p className="text-sm text-muted-foreground">
            开发新功能时，在 src/pages/ 下新建页面，然后在 src/App.tsx
            中注册路由即可；UI 组件可直接使用 src/components/ui/ 下的
            shadcn 风格组件。
          </p>
          <Button variant="outline" asChild>
            <a href="https://ui.shadcn.com/docs" target="_blank" rel="noreferrer">
              查看 shadcn/ui 文档
            </a>
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
