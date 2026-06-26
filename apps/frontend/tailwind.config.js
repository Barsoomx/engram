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
            background: '#0B0F14',
            foreground: '#ECEDEE',
          },
        },
      },
    }),
  ],
};

module.exports = config;
