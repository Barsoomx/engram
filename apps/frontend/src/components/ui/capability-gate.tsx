'use client';

import { useQuery } from '@tanstack/react-query';
import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';

export interface CapabilityGateProps {
  capabilities: string[];
  required: string;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

export function CapabilityGate({
  capabilities,
  required,
  children,
  fallback,
}: CapabilityGateProps) {
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  if (meQuery.isPending) {

    return null;
  }

  const resolved = meQuery.data?.capabilities ?? capabilities;

  if (!hasCapability(resolved, required)) {

    return <>{fallback ?? <EmptyState title='Insufficient permissions' />}</>;
  }

  return <>{children}</>;
}
