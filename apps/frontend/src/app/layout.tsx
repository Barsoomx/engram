import '@/styles/globals.css';
import clsx from 'clsx';
import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import type { ReactNode } from 'react';

import { Providers } from './providers';

const geistSans = Geist({
  subsets: ['latin'],
  variable: '--font-sans',
  display: 'swap',
});

const geistMono = Geist_Mono({
  subsets: ['latin'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Engram Console',
  description: 'Engram — engineering memory for AI coding agents',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      className={clsx('dark', geistSans.variable, geistMono.variable)}
      lang='en'
      suppressHydrationWarning
    >
      <head>
        <meta content='dark' name='color-scheme' />
        <meta name='darkreader-lock' />
      </head>
      <body className='font-sans antialiased'>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
