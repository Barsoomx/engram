'use client';

import * as React from 'react';

import { KIND_STYLES, resolveKind } from '@/lib/design';

export interface KindBadgeProps {
  kind: string | null | undefined;
  className?: string;
}

export function KindBadge({ kind, className }: KindBadgeProps) {
  const style = KIND_STYLES[resolveKind(kind)];

  return (
    <span
      className={`inline-flex items-center rounded-[7px] px-2 py-0.5 text-[11px] font-medium ${className ?? ''}`}
      style={{ color: style.text, backgroundColor: style.bg }}
    >
      {style.label}
    </span>
  );
}

export interface KindDotProps {
  kind: string | null | undefined;
  size?: number;
  className?: string;
}

export function KindDot({ kind, size = 8, className }: KindDotProps) {
  const style = KIND_STYLES[resolveKind(kind)];

  return (
    <span
      className={`inline-block shrink-0 rounded-[3px] ${className ?? ''}`}
      style={{ width: size, height: size, backgroundColor: style.dot }}
    />
  );
}
