import type { Metadata } from 'next';
import type { ReactNode } from 'react';

export const metadata: Metadata = {
  title: 'Engram Admin',
  description: 'Engram admin console',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang='en'>
      <body>
        <header
          style={{
            padding: '1rem',
            borderBottom: '1px solid #e5e5e5',
            display: 'flex',
            gap: '1rem',
          }}
        >
          <strong>Engram Admin</strong>
          <nav style={{ display: 'flex', gap: '1rem' }}>
            <a href='/'>Home</a>
            <a href='/health'>Health</a>
            <a href='/memories'>Memories</a>
          </nav>
        </header>
        <main style={{ padding: '1rem', fontFamily: 'sans-serif' }}>
          {children}
        </main>
      </body>
    </html>
  );
}
