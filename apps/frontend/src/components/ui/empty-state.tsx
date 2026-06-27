'use client';

import { Inbox } from 'lucide-react';
import * as React from 'react';

export interface EmptyStateProps {
  title: string;
  description?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
}

export function EmptyState({ title, description, icon, action }: EmptyStateProps) {
  return (
    <div className='surface-card flex flex-col items-center justify-center px-6 py-12 text-center'>
      <div className='w-12 h-12 rounded-full bg-content2 flex items-center justify-center text-default-400 mb-4'>
        {icon ?? <Inbox className='w-6 h-6' />}
      </div>
      <h3 className='text-base font-semibold text-foreground'>{title}</h3>
      {description && (
        <p className='mt-1 max-w-sm text-sm text-default-500'>{description}</p>
      )}
      {action && <div className='mt-4'>{action}</div>}
    </div>
  );
}
