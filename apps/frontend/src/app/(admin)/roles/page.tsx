'use client';

import { Chip } from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import { ShieldCheck } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { useRoles } from '@/hooks/use-members';
import { fetchMe, type MeResponse } from '@/lib/auth';
import type { Role } from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';

function CapabilityChips({ capabilities }: { capabilities: string[] }) {
  return (
    <div className='flex flex-wrap gap-1.5'>
      {capabilities.map((capability) => (
        <Chip
          key={capability}
          size='sm'
          variant='bordered'
          className='font-mono text-xs'
        >
          {capability}
        </Chip>
      ))}
    </div>
  );
}

function RolesTable({ items }: { items: Role[] }) {
  return (
    <ResponsiveTable>
      <thead>
        <tr className='border-b border-divider'>
          <th className='py-2 px-3 text-default-500 font-medium'>Code</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Name</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Description</th>
          <th className='hidden py-2 px-3 text-default-500 font-medium md:table-cell'>
            Capabilities
          </th>
        </tr>
      </thead>
      <tbody>
        {items.map((role) => (
          <tr key={role.id} className='border-b border-divider/50 align-top'>
            <td className='py-2 px-3 font-mono text-xs text-default-700 whitespace-nowrap'>
              {role.code}
            </td>
            <td className='py-2 px-3 text-foreground'>
              <span>{role.name}</span>
              {role.capabilities.length > 0 && (
                <div className='mt-1.5 md:hidden'>
                  <CapabilityChips capabilities={role.capabilities} />
                </div>
              )}
            </td>
            <td className='py-2 px-3 max-w-[360px]'>
              {role.description ? (
                <span className='text-default-600'>{role.description}</span>
              ) : (
                <span className='text-default-400 text-xs'>—</span>
              )}
            </td>
            <td className='hidden py-2 px-3 md:table-cell'>
              {role.capabilities.length === 0 ? (
                <span className='text-default-400 text-xs'>—</span>
              ) : (
                <CapabilityChips capabilities={role.capabilities} />
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </ResponsiveTable>
  );
}

export default function RolesPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const params = React.useMemo(() => ({ page: 1, pageSize: 50 }), []);
  const rolesQuery = useRoles(activeOrgId, params);

  const isLoading = meQuery.isLoading || rolesQuery.isLoading;
  const items = rolesQuery.data?.results ?? [];

  return (
    <CapabilityGate capabilities={capabilities} required='roles:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Roles'
          subtitle='Roles group capabilities that can be assigned to members.'
        />

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: 4 }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={4} />
            </table>
          ) : rolesQuery.isError ? (
            <ErrorState
              message={
                rolesQuery.error instanceof Error
                  ? rolesQuery.error.message
                  : 'Failed to load roles.'
              }
              onRetry={() => rolesQuery.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              title='No roles'
              description='No roles are available in this organization.'
              icon={<ShieldCheck className='w-6 h-6' />}
            />
          ) : (
            <RolesTable items={items} />
          )}
        </div>

        {!rolesQuery.isError && items.length > 0 && (
          <div className='flex items-center justify-between text-xs text-default-500'>
            <p>
              Showing {items.length} role{items.length === 1 ? '' : 's'}.
            </p>
            <p>Read-only.</p>
          </div>
        )}
      </section>
    </CapabilityGate>
  );
}
