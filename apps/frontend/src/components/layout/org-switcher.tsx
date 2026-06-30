'use client';

import { Building2, Plus, Settings } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import {
  DropdownDivider,
  DropdownEyebrow,
  DropdownPanel,
  MenuActionRow,
  MenuRow,
  SwitcherBackdrop,
  SwitcherTrigger,
} from '@/components/layout/switcher-ui';
import { InitialTile } from '@/components/ui/initial-tile';
import { useOrganizations } from '@/hooks/use-organizations';
import { useOrgStore } from '@/lib/org-store';
import { useSwitcherStore } from '@/lib/switcher-store';

function formatOrgMeta(
  viewer_role: string | null | undefined,
  member_count: number | null | undefined,
  slug: string,
): string {
  if (viewer_role == null && member_count == null) {
    return slug;
  }

  const parts: string[] = [];

  if (viewer_role != null) {
    const humanized = viewer_role.replace(/^organization_/, '').replace(/_/g, ' ');
    parts.push(humanized.charAt(0).toUpperCase() + humanized.slice(1));
  }

  if (member_count != null) {
    parts.push(`${member_count} ${member_count === 1 ? 'member' : 'members'}`);
  }

  return parts.length > 0 ? parts.join(' · ') : slug;
}

export interface OrgSwitcherProps {
  orgId: string | null;
}

export function OrgSwitcher({ orgId }: OrgSwitcherProps) {
  const router = useRouter();
  const query = useOrganizations(orgId);
  const setActiveOrg = useOrgStore((state) => state.setActiveOrg);
  const open = useSwitcherStore((s) => s.openMenu === 'org');
  const toggle = useSwitcherStore((s) => s.toggle);
  const close = useSwitcherStore((s) => s.close);
  const data = query.data;

  React.useEffect(() => {
    if (!query.isSuccess || !data) {
      return;
    }

    const current = useOrgStore.getState().activeOrgId;

    if (!current || !data.results.some((org) => org.id === current)) {
      setActiveOrg(data.results[0]?.id ?? null);
    }
  }, [query.isSuccess, data, setActiveOrg]);

  const organizations = data?.results ?? [];
  const activeOrg =
    organizations.find((org) => org.id === orgId) ?? organizations[0] ?? null;

  if (query.isLoading || !activeOrg) {
    return (
      <div className='flex items-center gap-2 px-2 text-[13px] text-default-500'>
        <Building2 className='h-4 w-4' />
        <span>{query.isLoading ? 'Loading…' : 'No organizations'}</span>
      </div>
    );
  }

  return (
    <div className='relative'>
      {open && <SwitcherBackdrop onClose={close} />}

      <SwitcherTrigger active={open} onClick={() => toggle('org')}>
        <InitialTile name={activeOrg.name} size={22} variant='gradient' />
        <span className='truncate text-[13.5px] font-semibold text-foreground'>
          {activeOrg.name}
        </span>
      </SwitcherTrigger>

      {open && (
        <DropdownPanel width={288}>
          <DropdownEyebrow>Organizations</DropdownEyebrow>
          <div className='max-h-[300px] space-y-0.5 overflow-y-auto'>
            {organizations.map((org) => (
              <MenuRow
                key={org.id}
                active={org.id === activeOrg.id}
                onClick={() => {
                  setActiveOrg(org.id);
                  close();
                }}
              >
                <InitialTile name={org.name} size={28} variant='gradient' />
                <div className='min-w-0 flex-1'>
                  <div className='truncate text-[13px] font-semibold text-foreground'>
                    {org.name}
                  </div>
                  <div className='truncate font-mono text-[11px] text-default-400'>
                    {formatOrgMeta(org.viewer_role, org.member_count, org.slug)}
                  </div>
                </div>
              </MenuRow>
            ))}
          </div>
          <DropdownDivider />
          <MenuActionRow
            icon={Plus}
            label='Create organization'
            onClick={() => {
              close();
              router.push('/organizations');
            }}
          />
          <MenuActionRow
            icon={Settings}
            label='Organization settings'
            onClick={() => {
              close();
              router.push('/organizations');
            }}
          />
        </DropdownPanel>
      )}
    </div>
  );
}
