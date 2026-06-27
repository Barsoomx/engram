'use client';

import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { hasCapability } from '@/lib/auth';

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
  const allowed = hasCapability(capabilities, required);

  if (!allowed) {

    return (
      <>{fallback ?? <EmptyState title='Insufficient permissions' />}</>
    );
  }

  return <>{children}</>;
}
