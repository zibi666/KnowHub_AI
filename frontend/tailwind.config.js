/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        ink: 'var(--text)',
        panel: 'var(--panel-bg)',
        line: 'var(--line)'
      }
    }
  },
  plugins: []
}
