'use client';

import { ToastProvider } from '@heroui/react';
import { HeroUIProvider } from '@heroui/system';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { getToken } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';

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

  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const [activeToken, setActiveToken] = React.useState<string | null>(null);

  React.useEffect(() => {
    setActiveToken(getToken());
  }, []);

  React.useEffect(() => {
    const syncToken = (): void => {
      setActiveToken(getToken());
    };

    window.addEventListener('storage', syncToken);

    return () => {
      window.removeEventListener('storage', syncToken);
    };
  }, []);

  React.useEffect(() => {
    queryClient.clear();
  }, [activeOrgId, activeToken, queryClient]);

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
