'use client';

import { Select, SelectItem } from '@heroui/react';
import { Building2, Loader2 } from 'lucide-react';
import * as React from 'react';

import { useOrganizations } from '@/hooks/use-organizations';
import { useOrgStore } from '@/lib/org-store';

export interface OrgSwitcherProps {
  orgId: string | null;
}

export function OrgSwitcher({ orgId }: OrgSwitcherProps) {
  const query = useOrganizations(orgId, { enabled: Boolean(orgId) });
  const setActiveOrg = useOrgStore((state) => state.setActiveOrg);
  const data = query.data;

  React.useEffect(() => {
    if (!query.isSuccess || !data || data.results.length === 0) {

      return;
    }

    if (!useOrgStore.getState().activeOrgId) {
      setActiveOrg(data.results[0].id);
    }
  }, [query.isSuccess, data, setActiveOrg]);

  const organizations = data?.results ?? [];

  if (query.isLoading) {

    return (
      <div className='flex items-center gap-2 text-sm text-default-500'>
        <Loader2 className='w-4 h-4 animate-spin' />
        <span>Loading organizations…</span>
      </div>
    );
  }

  if (query.isError || organizations.length === 0) {

    return (
      <div className='flex items-center gap-2 text-sm text-default-500'>
        <Building2 className='w-4 h-4' />
        <span>No organizations</span>
      </div>
    );
  }

  return (
    <Select
      aria-label='Active organization'
      classNames={{
        base: 'w-[220px]',
        trigger: 'h-9 min-h-9 bg-content2/60 border border-divider',
      }}
      items={organizations}
      labelPlacement='outside'
      selectedKeys={orgId ? new Set([orgId]) : new Set()}
      startContent={<Building2 className='w-4 h-4 text-default-500' />}
      variant='bordered'
      onSelectionChange={(keys) => {
        const next = Array.from(keys)[0];

        if (typeof next === 'string') {
          setActiveOrg(next);
        }
      }}
    >
      {(org) => (
        <SelectItem key={org.id}>{org.name}</SelectItem>
      )}
    </Select>
  );
}
