import { render, screen } from '@testing-library/react'
import { describe, expect, it, beforeEach } from 'vitest'
import App from './App'

describe('App', () => {
  beforeEach(() => {
    // BrowserRouter basename="/vllm_manager/frontend/" 需要匹配的 URL 前缀
    window.history.pushState({}, '', '/vllm_manager/frontend/login')
  })

  it('renders the app shell without crashing', async () => {
    const { container } = render(<App />)

    // 未登录时默认落在登录页（懒加载完成后应出现登录表单的提交按钮）
    expect(
      await screen.findByRole('button', { name: /登录/ })
    ).toBeInTheDocument()

    // 懒加载完成后挂载了真实内容
    expect(container.firstChild).not.toBeNull()
  })
})
