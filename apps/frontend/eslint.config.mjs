import nextCoreWebVitals from 'eslint-config-next/core-web-vitals'

const eslintConfig = [
  ...nextCoreWebVitals,
  {
    ignores: ['.next/**', 'node_modules/**'],
  },
  {
    rules: {
      // eslint-config-next 16 newly enables this react-compiler-oriented rule;
      // our deliberate reset-on-scope effects (e.g. reset pagination on org/project
      // change) trip it. Keep the prior behavior — a real refactor is out of scope
      // for a dependency bump.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
]

export default eslintConfig
