'use client';

import * as React from 'react';

import { statusTone, TONE_STYLES, type StatusTone } from '@/lib/design';

export interface StatusPillProps {
  status: string | null | undefined;
  tone?: StatusTone;
  label?: string;
  withDot?: boolean;
  className?: string;
}

function humanize(value: string | null | undefined): string {
  const cleaned = (value ?? '').trim();

  if (!cleaned) {
    return '—';
  }

  const spaced = cleaned.replace(/[_-]+/g, ' ');

  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function StatusPill({
  status,
  tone,
  label,
  withDot = true,
  className,
}: StatusPillProps) {
  const resolvedTone = tone ?? statusTone(status);
  const style = TONE_STYLES[resolvedTone];

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[7px] px-2 py-0.5 text-[11px] font-medium ${className ?? ''}`}
      style={{ color: style.text, backgroundColor: style.bg }}
    >
      {withDot && (
        <span
          className='inline-block h-1.5 w-1.5 shrink-0 rounded-full'
          style={{ backgroundColor: style.dot }}
        />
      )}
      {label ?? humanize(status)}
    </span>
  );
}
