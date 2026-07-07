'use client';

import { CopyButton } from '@/components/ui/copy-button';

export interface CopyableIdProps {
  value: string;
  display?: string;
  className?: string;
}

export function CopyableId({ value, display, className }: CopyableIdProps) {
  return (
    <span
      title={value}
      className={`inline-flex min-w-0 items-center gap-1.5 font-mono text-[11.5px] text-default-500 ${className ?? ''}`}
    >
      <span className='truncate'>{display ?? value}</span>
      <CopyButton value={value} size={12} />
    </span>
  );
}
