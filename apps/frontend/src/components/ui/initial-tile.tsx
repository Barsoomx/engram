'use client';

import * as React from 'react';

import { avatarColor, avatarGradient, initials } from '@/lib/design';

export interface InitialTileProps {
  name: string;
  seed?: string;
  size?: number;
  variant?: 'gradient' | 'flat';
  radius?: number;
  className?: string;
}

export function InitialTile({
  name,
  seed,
  size = 32,
  variant = 'gradient',
  radius,
  className,
}: InitialTileProps) {
  const key = seed ?? name;
  const background =
    variant === 'gradient' ? avatarGradient(key) : avatarColor(key);
  const fontSize = Math.max(9, Math.round(size * 0.4));

  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center font-semibold text-white ${className ?? ''}`}
      style={{
        width: size,
        height: size,
        borderRadius: radius ?? Math.round(size * 0.28),
        backgroundImage: variant === 'gradient' ? background : undefined,
        backgroundColor: variant === 'flat' ? background : undefined,
        fontSize,
        letterSpacing: '0.01em',
      }}
    >
      {initials(name)}
    </span>
  );
}
