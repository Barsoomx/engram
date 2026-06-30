'use client';

import * as React from 'react';

export interface PageHeaderProps {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}

export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <div className='flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between'>
      <div className='min-w-0 space-y-1.5'>
        <h1 className='text-[25px] font-semibold leading-[1.2] tracking-[-0.02em] text-foreground'>
          {title}
        </h1>
        {subtitle && (
          <p className='text-[13.5px] leading-relaxed text-default-500'>
            {subtitle}
          </p>
        )}
      </div>
      {actions && (
        <div className='flex shrink-0 items-center gap-2'>{actions}</div>
      )}
    </div>
  );
}
