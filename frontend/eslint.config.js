import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // async init-fetch 模式（挂载时 await 后 setState）会被该规则误报，
      // 样板工程的 UserContext 采用标准 fetch-on-mount，关闭此项
      'react-hooks/set-state-in-effect': 'off',
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
  {
    // Context 文件（Provider 组件 + useXxx hook 同文件）是 React 标准模式，
    // fast-refresh 规则无法识别，属误报
    files: ['src/contexts/**/*.tsx', 'src/lib/theme-provider.tsx'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
)
