'use client';

import { Check, Copy } from 'lucide-react';
import * as React from 'react';

export interface CopyButtonProps {
  value: string;
  size?: number;
  className?: string;
  label?: string;
}

export function CopyButton({ value, size = 13, className, label = 'Copy' }: CopyButtonProps) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout>>();

  React.useEffect(() => () => clearTimeout(timer.current), []);

  const handleCopy = React.useCallback(() => {
    if (!navigator.clipboard) {
      return;
    }

    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  }, [value]);

  return (
    <button
      type='button'
      onClick={handleCopy}
      title={copied ? 'Copied' : label}
      aria-label={label}
      className={`inline-flex shrink-0 items-center text-default-400 transition-colors hover:text-foreground ${className ?? ''}`}
    >
      {copied ? (
        <Check size={size} strokeWidth={2.2} className='text-success' />
      ) : (
        <Copy size={size} strokeWidth={2} />
      )}
    </button>
  );
}
