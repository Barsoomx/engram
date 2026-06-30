'use client';

import * as React from 'react';

export interface PulseDotProps {
  color?: string;
  size?: number;
  pulse?: boolean;
  className?: string;
}

export function PulseDot({
  color = '#3DD9AC',
  size = 7,
  pulse = true,
  className,
}: PulseDotProps) {
  return (
    <span
      className={`inline-block shrink-0 rounded-full ${pulse ? 'pulse-dot' : ''} ${className ?? ''}`}
      style={{
        width: size,
        height: size,
        backgroundColor: color,
        boxShadow: `0 0 0 3px ${color}22`,
      }}
    />
  );
}
