'use client';

import * as React from 'react';

export interface SparklineProps {
  data: number[];
  color?: string;
  height?: number;
  strokeWidth?: number;
  fill?: boolean;
  className?: string;
}

export function Sparkline({
  data,
  color = '#7C5CFF',
  height = 26,
  strokeWidth = 1.8,
  fill = false,
  className,
}: SparklineProps) {
  const width = 100;
  const gradientId = React.useId();

  const { line, area } = React.useMemo(() => {
    if (data.length === 0) {
      return { line: '', area: '' };
    }

    const min = Math.min(...data);
    const max = Math.max(...data);
    const span = max - min || 1;
    const stepX = data.length > 1 ? width / (data.length - 1) : 0;
    const pad = strokeWidth;

    const points = data.map((value, index) => {
      const x = index * stepX;
      const y =
        height - pad - ((value - min) / span) * (height - pad * 2);

      return [x, y] as const;
    });

    const linePath = points
      .map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`)
      .join(' ');

    const areaPath = `${linePath} L${width},${height} L0,${height} Z`;

    return { line: linePath, area: areaPath };
  }, [data, height, strokeWidth]);

  return (
    <svg
      className={className}
      width='100%'
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio='none'
      fill='none'
      aria-hidden='true'
    >
      {fill && (
        <>
          <defs>
            <linearGradient id={gradientId} x1='0' y1='0' x2='0' y2='1'>
              <stop offset='0%' stopColor={color} stopOpacity='0.22' />
              <stop offset='100%' stopColor={color} stopOpacity='0' />
            </linearGradient>
          </defs>
          <path d={area} fill={`url(#${gradientId})`} />
        </>
      )}
      <path
        d={line}
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinecap='round'
        strokeLinejoin='round'
        vectorEffect='non-scaling-stroke'
      />
    </svg>
  );
}
