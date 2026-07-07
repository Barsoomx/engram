'use client';

import { Button } from '@heroui/react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import * as React from 'react';

export interface ErrorStateProps {
  message: string;
  title?: string;
  onRetry?: () => void;
  icon?: React.ReactNode;
}

export function ErrorState({
  message,
  title = 'Something went wrong',
  onRetry,
  icon,
}: ErrorStateProps) {
  return (
    <div className='surface-card flex flex-col items-center justify-center px-6 py-12 text-center'>
      <div className='w-12 h-12 rounded-full bg-danger-500/10 flex items-center justify-center text-danger-500 mb-4'>
        {icon ?? <AlertTriangle className='w-6 h-6' />}
      </div>
      <h3 className='text-base font-semibold text-foreground'>{title}</h3>
      <p className='mt-1 max-w-sm text-sm text-default-500'>{message}</p>
      {onRetry && (
        <div className='mt-4'>
          <Button
            size='sm'
            variant='flat'
            startContent={<RefreshCw className='w-3.5 h-3.5' />}
            onPress={onRetry}
          >
            Retry
          </Button>
        </div>
      )}
    </div>
  );
}
