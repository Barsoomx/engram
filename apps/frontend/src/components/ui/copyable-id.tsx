'use client';

import { Check, Copy } from 'lucide-react';
import * as React from 'react';

export interface CopyableIdProps {
  value: string;
  display?: string;
  className?: string;
}

export function CopyableId({ value, display, className }: CopyableIdProps) {
  const [copied, setCopied] = React.useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button
      type='button'
      onClick={copy}
      title={value}
      className={`inline-flex min-w-0 items-center gap-1.5 font-mono text-[11.5px] text-default-500 transition-colors hover:text-foreground ${className ?? ''}`}
    >
      <span className='truncate'>{display ?? value}</span>
      {copied ? (
        <Check size={12} strokeWidth={2.5} className='shrink-0 text-success' />
      ) : (
        <Copy size={12} strokeWidth={1.8} className='shrink-0 text-default-400' />
      )}
    </button>
  );
}
