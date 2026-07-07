'use client';

import * as React from 'react';

export interface ResponsiveTableProps {
  children: React.ReactNode;
  minWidth?: number | string;
  className?: string;
}

export function ResponsiveTable({
  children,
  minWidth = 640,
  className,
}: ResponsiveTableProps) {
  const resolvedMinWidth =
    typeof minWidth === 'number' ? `${minWidth}px` : minWidth;

  return (
    <div className='overflow-x-auto'>
      <table
        className={`w-full border-collapse text-left text-sm ${className ?? ''}`}
        style={{ minWidth: resolvedMinWidth }}
      >
        {children}
      </table>
    </div>
  );
}
