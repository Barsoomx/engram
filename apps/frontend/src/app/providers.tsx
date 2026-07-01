'use client';

import { ToastProvider } from '@heroui/react';
import { HeroUIProvider } from '@heroui/system';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { getToken } from '@/lib/auth';

export interface ProvidersProps {
  children: React.ReactNode;
}

export function Providers({ children }: ProvidersProps) {
  const router = useRouter();
  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );

  const [activeToken, setActiveToken] = React.useState<string | null>(null);
  const previousTokenRef = React.useRef<string | null>(null);

  React.useEffect(() => {
    setActiveToken(getToken());
  }, []);

  React.useEffect(() => {
    const syncToken = (): void => {
      setActiveToken(getToken());
    };

    window.addEventListener('storage', syncToken);
    window.addEventListener('engram:token', syncToken);

    return () => {
      window.removeEventListener('storage', syncToken);
      window.removeEventListener('engram:token', syncToken);
    };
  }, []);

  React.useEffect(() => {
    const previousToken = previousTokenRef.current;
    previousTokenRef.current = activeToken;

    if (previousToken !== null && previousToken !== activeToken) {
      queryClient.clear();
    }
  }, [activeToken, queryClient]);

  return (
    <HeroUIProvider navigate={router.push}>
      <ToastProvider
        maxVisibleToasts={1}
        placement='top-center'
        toastProps={{
          timeout: 5000,
          shouldShowTimeoutProgress: true,
        }}
      />
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </HeroUIProvider>
  );
}
