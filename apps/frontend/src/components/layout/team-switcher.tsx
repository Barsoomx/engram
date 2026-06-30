'use client';

import { Users } from 'lucide-react';
import * as React from 'react';

import {
  DropdownEyebrow,
  DropdownPanel,
  MenuRow,
  SwitcherBackdrop,
  SwitcherTrigger,
} from '@/components/layout/switcher-ui';
import { InitialTile } from '@/components/ui/initial-tile';
import { useTeams } from '@/hooks/use-teams';
import { useOrgStore } from '@/lib/org-store';
import { useSwitcherStore } from '@/lib/switcher-store';
import { useTeamStore } from '@/lib/team-store';

export function TeamSwitcher() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const activeTeamId = useTeamStore((state) => state.activeTeamId);
  const setActiveTeam = useTeamStore((state) => state.setActiveTeam);
  const open = useSwitcherStore((s) => s.openMenu === 'team');
  const toggle = useSwitcherStore((s) => s.toggle);
  const close = useSwitcherStore((s) => s.close);

  const query = useTeams(
    activeOrgId,
    { pageSize: 100 },
    { enabled: Boolean(activeOrgId) },
  );
  const data = query.data;

  React.useEffect(() => {
    if (!query.isSuccess || !data || activeTeamId === null) {
      return;
    }

    const ids = data.results.map((t) => t.id);

    if (!ids.includes(activeTeamId)) {
      setActiveTeam(null);
    }
  }, [query.isSuccess, data, activeTeamId, setActiveTeam]);

  const teams = data?.results ?? [];
  const activeTeam = teams.find((t) => t.id === activeTeamId) ?? null;
  const label = activeTeam ? activeTeam.name : 'All teams';

  if (query.isLoading) {
    return (
      <div className='flex items-center gap-2 px-2 text-[13px] text-default-500'>
        <Users className='h-4 w-4' />
        <span>Loading…</span>
      </div>
    );
  }

  return (
    <div className='relative'>
      {open && <SwitcherBackdrop onClose={close} />}

      <SwitcherTrigger active={open} onClick={() => toggle('team')}>
        <Users size={15} strokeWidth={1.8} className='shrink-0 text-default-400' />
        <span className='truncate text-[13px] font-medium text-foreground'>
          {label}
        </span>
      </SwitcherTrigger>

      {open && (
        <DropdownPanel width={240}>
          <DropdownEyebrow>Teams</DropdownEyebrow>
          <div className='max-h-[300px] space-y-0.5 overflow-y-auto'>
            <MenuRow
              active={activeTeamId === null}
              onClick={() => {
                setActiveTeam(null);
                close();
              }}
            >
              <span className='flex h-6 w-6 shrink-0 items-center justify-center rounded-[7px] bg-content3 text-default-500'>
                <Users size={13} strokeWidth={1.8} />
              </span>
              <span className='flex-1 text-[13px] font-medium text-foreground'>
                All teams
              </span>
            </MenuRow>
            {teams.map((team) => (
              <MenuRow
                key={team.id}
                active={team.id === activeTeamId}
                onClick={() => {
                  setActiveTeam(team.id);
                  close();
                }}
              >
                <InitialTile
                  name={team.name}
                  seed={team.slug}
                  size={24}
                  variant='flat'
                />
                <span className='min-w-0 flex-1 truncate text-[13px] font-medium text-foreground'>
                  {team.name}
                </span>
              </MenuRow>
            ))}
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}
