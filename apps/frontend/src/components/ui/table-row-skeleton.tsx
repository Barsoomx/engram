'use client';

import * as React from 'react';

export interface TableRowSkeletonProps {
  columns: number;
  rows?: number;
}

export function TableRowSkeleton({ columns, rows = 5 }: TableRowSkeletonProps) {
  const rowList = React.useMemo(() => Array.from({ length: rows }), [rows]);
  const colList = React.useMemo(
    () => Array.from({ length: columns }),
    [columns],
  );

  return (
    <tbody aria-busy='true' aria-live='polite'>
      {rowList.map((_, rowIndex) => (
        <tr key={`skeleton-row-${rowIndex}`}>
          {colList.map((__, colIndex) => (
            <td
              key={`skeleton-cell-${rowIndex}-${colIndex}`}
              className='py-3 px-4'
            >
              <div className='h-4 w-full max-w-[180px] rounded-medium bg-content2 animate-pulse' />
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  );
}
