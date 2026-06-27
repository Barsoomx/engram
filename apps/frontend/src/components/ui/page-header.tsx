'use client';

import * as React from 'react';

export interface PageHeaderProps {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}

export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <div className='flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between'>
      <div className='min-w-0 space-y-1'>
        <h1 className='text-2xl font-semibold text-foreground'>{title}</h1>
        {subtitle && (
          <p className='text-sm text-default-500'>{subtitle}</p>
        )}
      </div>
      {actions && <div className='flex shrink-0 items-center gap-2'>{actions}</div>}
    </div>
  );
}
