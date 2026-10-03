/** @type {import('tailwindcss').Config} */
import animate from 'tailwindcss-animate'

export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        brand: {
          DEFAULT: 'hsl(var(--brand))',
          foreground: 'hsl(var(--brand-foreground))',
          2: 'hsl(var(--brand-2))',
          3: 'hsl(var(--brand-3))',
        },
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        free: {
          DEFAULT: 'hsl(var(--free))',
          foreground: 'hsl(var(--free-foreground))',
        },
        paid: 'hsl(var(--paid))',
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 3px)',
        sm: 'calc(var(--radius) - 5px)',
      },
      // A single elevation ladder, so surfaces stack consistently instead of
      // every component inventing its own blur and offset.
      boxShadow: {
        xs: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
        soft: '0 1px 2px -1px rgb(0 0 0 / 0.10), 0 2px 6px -2px rgb(0 0 0 / 0.12)',
        pop: '0 2px 4px -2px rgb(0 0 0 / 0.16), 0 10px 28px -10px rgb(0 0 0 / 0.32)',
        lift: '0 1px 2px -1px rgb(0 0 0 / 0.12), 0 16px 40px -16px rgb(0 0 0 / 0.40)',
        glow: '0 0 25px -6px hsl(var(--brand) / 0.30)',
        'glow-free': '0 0 22px -5px hsl(var(--free) / 0.28)',
        'glow-sm': '0 0 14px -3px hsl(var(--brand) / 0.35)',
      },
      // v4 ships a dynamic spacing scale, so `py-1.75` / `w-49` / `gap-4.5` all
      // resolve. v3 only has the multiples below, so add the ones the pasted
      // component relies on instead of rewriting every class.
      spacing: {
        '1.25': '0.3125rem',
        '1.75': '0.4375rem',
        '2.75': '0.6875rem',
        '4.5': '1.125rem',
        '5.5': '1.375rem',
        49: '12.25rem',
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      // The component uses a custom out-curve; naming it beats escaping an
      // arbitrary value that Tailwind cannot parse.
      transitionTimingFunction: {
        swift: 'cubic-bezier(0.23,1,0.32,1)',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'none' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'scale-in': {
          from: { opacity: '0', transform: 'scale(0.96)' },
          to: { opacity: '1', transform: 'scale(1)' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
        'pulse-subtle': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.65', transform: 'scale(0.96)' },
        },
      },
      animation: {
        'fade-up': 'fade-up .22s ease-out both',
        'fade-in': 'fade-in .18s ease-out both',
        'scale-in': 'scale-in .2s cubic-bezier(0.23,1,0.32,1) both',
        shimmer: 'shimmer 1.6s infinite',
        'pulse-subtle': 'pulse-subtle 2.5s ease-in-out infinite',
      },
    },
  },
  plugins: [animate],
}
