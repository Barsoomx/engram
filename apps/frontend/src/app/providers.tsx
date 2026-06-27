'use client';

import { ToastProvider } from '@heroui/react';
import { HeroUIProvider } from '@heroui/system';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import * as React from 'react';

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
