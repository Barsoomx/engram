import '@/styles/globals.css';
import clsx from 'clsx';
import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import { Providers } from './providers';

export const metadata: Metadata = {
  title: 'Engram Admin',
  description: 'Engram admin console',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html className='dark' lang='en' suppressHydrationWarning>
      <head>
        <meta content='dark' name='color-scheme' />
        <meta name='darkreader-lock' />
      </head>
      <body className={clsx('font-sans antialiased')}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
