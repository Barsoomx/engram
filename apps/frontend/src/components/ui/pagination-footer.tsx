'use client';

import { Pagination } from '@heroui/react';
import * as React from 'react';

export interface PaginationFooterProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  noun?: string;
  isDisabled?: boolean;
}

export function PaginationFooter({
  page,
  pageSize,
  total,
  onPageChange,
  noun = 'item',
  isDisabled = false,
}: PaginationFooterProps) {
  if (total <= 0) {
    return null;
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <div className='flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between'>
      <p className='text-xs text-default-500'>
        Showing {first}–{last} of {total} {noun}
        {total === 1 ? '' : 's'}.
      </p>
      {totalPages > 1 && (
        <Pagination
          total={totalPages}
          page={page}
          onChange={onPageChange}
          size='sm'
          isDisabled={isDisabled}
        />
      )}
    </div>
  );
}
