'use client';

import * as React from 'react';

export interface ConfidenceTrackProps {
  value: number;
  width?: number;
  height?: number;
  className?: string;
}

export function ConfidenceTrack({
  value,
  width = 54,
  height = 7,
  className,
}: ConfidenceTrackProps) {
  const pct = Math.max(0, Math.min(100, value));

  return (
    <span
      className={`inline-block overflow-hidden rounded-full ${className ?? ''}`}
      style={{
        width,
        height,
        backgroundColor: 'rgba(255,255,255,0.08)',
      }}
    >
      <span
        className='block h-full rounded-full'
        style={{
          width: `${pct}%`,
          backgroundImage: 'linear-gradient(90deg,#6A4DFF,#A78BFF)',
        }}
      />
    </span>
  );
}
