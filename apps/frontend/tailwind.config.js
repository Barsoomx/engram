const { heroui } = require('@heroui/theme');

/** @type {import('tailwindcss').Config} */
const config = {
  content: [
    './src/**/*.{js,ts,jsx,tsx,mdx}',
    './node_modules/@heroui/theme/dist/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['var(--font-sans, ui-sans-serif, system-ui, sans-serif)'],
        mono: ['var(--font-mono, ui-monospace, monospace)'],
      },
      colors: {
        line: 'var(--line)',
        line2: 'var(--line2)',
        'divider-strong': 'var(--line2)',
        'primary-soft': 'var(--primary-soft)',
        'kind-decision': '#A78BFF',
        'kind-convention': '#3DD9AC',
        'kind-gotcha': '#F2B765',
        'kind-architecture': '#6BA6FF',
        info: '#6BA6FF',
      },
      boxShadow: {
        'primary-glow':
          '0 8px 22px -8px rgba(124,92,255,.6), inset 0 1px 0 rgba(255,255,255,.2)',
        dropdown: '0 26px 64px -22px rgba(0,0,0,.8)',
        'login-card': '0 30px 80px -30px rgba(0,0,0,.7)',
        'brand-tile':
          '0 6px 18px -4px rgba(124,92,255,.5), inset 0 1px 0 rgba(255,255,255,.25)',
      },
      backgroundImage: {
        'primary-gradient': 'linear-gradient(150deg,#8B6BFF,#6A4DFF)',
        'brand-gradient': 'linear-gradient(150deg,#9277FF,#5A3DF2)',
      },
      transitionTimingFunction: {
        premium: 'cubic-bezier(.22,1,.36,1)',
      },
    },
  },
  darkMode: 'class',
  plugins: [
    heroui({
      themes: {
        light: {
          colors: {
            background: '#FFFFFF',
            foreground: '#11181C',
          },
        },
        dark: {
          colors: {
            background: '#0A0C11',
            foreground: '#ECEEF2',
            focus: '#7C5CFF',
            overlay: '#05070B',
            divider: 'rgba(255,255,255,0.065)',
            content1: { DEFAULT: '#101319', foreground: '#ECEEF2' },
            content2: { DEFAULT: '#161A21', foreground: '#ECEEF2' },
            content3: { DEFAULT: '#1C212A', foreground: '#ECEEF2' },
            content4: { DEFAULT: '#232A35', foreground: '#ECEEF2' },
            default: {
              50: '#0F1217',
              100: '#161A21',
              200: '#1C212A',
              300: '#2A313C',
              400: '#666C77',
              500: '#9197A2',
              600: '#A6ACB6',
              700: '#C8CDD6',
              800: '#DFE2E8',
              900: '#ECEEF2',
              foreground: '#ECEEF2',
              DEFAULT: '#161A21',
            },
            primary: {
              50: '#16122B',
              100: '#1E1838',
              200: '#2C2356',
              300: '#A78BFF',
              400: '#9277FF',
              500: '#7C5CFF',
              600: '#6A4DFF',
              700: '#5A3DF2',
              800: '#4A30D6',
              900: '#3A248F',
              foreground: '#FFFFFF',
              DEFAULT: '#7C5CFF',
            },
            secondary: {
              DEFAULT: '#A78BFF',
              foreground: '#0A0C11',
            },
            success: {
              50: '#0B221C',
              100: '#0F2F27',
              200: '#16463A',
              300: '#1F6A57',
              400: '#31B791',
              500: '#3DD9AC',
              600: '#5FE0BB',
              700: '#86E8CC',
              800: '#B6F1E1',
              900: '#DCF8F0',
              foreground: '#06140F',
              DEFAULT: '#3DD9AC',
            },
            warning: {
              50: '#241A0C',
              100: '#332512',
              200: '#4E391B',
              300: '#785829',
              400: '#E0A24A',
              500: '#F2B765',
              600: '#F5C77F',
              700: '#F8D6A0',
              800: '#FAE6C4',
              900: '#FDF3E1',
              foreground: '#140C02',
              DEFAULT: '#F2B765',
            },
            danger: {
              50: '#2A1416',
              100: '#3D1B1E',
              200: '#5E282B',
              300: '#8A3A3E',
              400: '#E15A5F',
              500: '#FB6E72',
              600: '#FC8589',
              700: '#FDA1A4',
              800: '#FEC4C6',
              900: '#FFE2E3',
              foreground: '#160405',
              DEFAULT: '#FB6E72',
            },
          },
        },
      },
    }),
  ],
};

module.exports = config;
