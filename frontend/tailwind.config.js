/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        ink: '#202123',
        panel: '#f7f7f8',
        line: '#e5e5e5'
      }
    }
  },
  plugins: []
}
