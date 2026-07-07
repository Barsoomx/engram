'use client';

import { Tooltip } from '@heroui/react';
import * as React from 'react';

import { formatRelativeTime } from '@/lib/design';
import { formatAbsolute } from '@/lib/format-time';

export interface TimeStampProps {
  value: string | null | undefined;
  relative?: boolean;
  className?: string;
}

export function TimeStamp({ value, relative = true, className }: TimeStampProps) {
  const absolute = formatAbsolute(value);
  const display = relative ? formatRelativeTime(value) : absolute;

  if (!value) {
    return <span className={className}>{display}</span>;
  }

  return (
    <Tooltip content={absolute} placement='top' delay={200} closeDelay={0}>
      <time dateTime={value} className={className}>
        {display}
      </time>
    </Tooltip>
  );
}
