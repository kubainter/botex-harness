/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ground: 'var(--surface-ground)',
        panel: 'var(--surface-panel)',
        'panel-hover': 'var(--surface-panel-hover)',
        card: 'var(--surface-card)',
        'fg-primary': 'var(--fg-primary)',
        'fg-secondary': 'var(--fg-secondary)',
        'fg-muted': 'var(--fg-muted)',
        'border-subtle': 'var(--border-subtle)',
        'border-focus': 'var(--border-focus)',
        accent: 'var(--accent-primary)',
        'accent-hover': 'var(--accent-hover)',
        botex: {
          50: 'rgb(var(--botex-50) / <alpha-value>)',
          100: 'rgb(var(--botex-100) / <alpha-value>)',
          200: 'rgb(var(--botex-200) / <alpha-value>)',
          300: 'rgb(var(--botex-300) / <alpha-value>)',
          400: 'rgb(var(--botex-400) / <alpha-value>)',
          500: 'rgb(var(--botex-500) / <alpha-value>)',
          600: 'rgb(var(--botex-600) / <alpha-value>)',
          700: 'rgb(var(--botex-700) / <alpha-value>)',
          800: 'rgb(var(--botex-800) / <alpha-value>)',
          900: 'rgb(var(--botex-900) / <alpha-value>)',
          950: 'rgb(var(--botex-950) / <alpha-value>)',
        },
        brand: {
          blue: '#2563eb',
          cyan: '#06b6d4',
          emerald: '#10b981',
          amber: '#f59e0b',
          rose: '#f43f5e',
          purple: '#8b5cf6',
        }
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
      }
    },
  },
  plugins: [],
}
