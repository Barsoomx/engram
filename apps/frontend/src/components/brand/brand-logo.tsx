'use client';

import * as React from 'react';

export interface BrandLogoProps {
  size?: number;
  className?: string;
}

export function BrandMark({ size = 32, className }: BrandLogoProps) {
  const radius = Math.round(size * 0.31);

  return (
    <span
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: radius,
        backgroundImage: 'var(--brand-grad)',
        boxShadow:
          '0 6px 18px -4px rgba(124,92,255,.5), inset 0 1px 0 rgba(255,255,255,.25)',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
      }}
    >
      <svg
        viewBox='0 0 24 24'
        fill='none'
        width={Math.round(size * 0.62)}
        height={Math.round(size * 0.62)}
        aria-hidden='true'
      >
        <ellipse
          cx='12'
          cy='12'
          rx='9.2'
          ry='4.6'
          transform='rotate(-32 12 12)'
          stroke='#fff'
          strokeOpacity='.92'
          strokeWidth='1.6'
        />
        <circle cx='12' cy='12' r='3' fill='#fff' />
        <circle cx='19.6' cy='7.2' r='1.75' fill='#fff' />
      </svg>
    </span>
  );
}

export interface BrandLockupProps {
  size?: number;
  eyebrow?: string;
  className?: string;
}

export function BrandLockup({
  size = 32,
  eyebrow = 'Console',
  className,
}: BrandLockupProps) {
  return (
    <div className={`flex items-center gap-2.5 ${className ?? ''}`}>
      <BrandMark size={size} />
      <div className='leading-none'>
        <div className='text-[15px] font-semibold tracking-tight text-foreground'>
          Engram
        </div>
        <div className='mt-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-default-400'>
          {eyebrow}
        </div>
      </div>
    </div>
  );
}
