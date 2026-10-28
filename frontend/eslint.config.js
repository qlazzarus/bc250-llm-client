import js from '@eslint/js'; import tseslint from 'typescript-eslint'; import hooks from 'eslint-plugin-react-hooks';
export default [{ ignores: ['dist/**', 'vite.config.js', 'vite.config.d.ts'] }, js.configs.recommended, ...tseslint.configs.recommended, { plugins: {'react-hooks': hooks}, rules: {'react-hooks/rules-of-hooks':'error'} }];
