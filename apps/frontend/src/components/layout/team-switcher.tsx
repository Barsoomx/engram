'use client';

import { Select, SelectItem } from '@heroui/react';
import { Loader2, Users } from 'lucide-react';
import * as React from 'react';

import { useTeams } from '@/hooks/use-teams';
import { useOrgStore } from '@/lib/org-store';
import { useTeamStore } from '@/lib/team-store';

const ALL_TEAMS_KEY = 'all';

type TeamListItem = { id: string; name: string };

export function TeamSwitcher() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const activeTeamId = useTeamStore((state) => state.activeTeamId);
  const setActiveTeam = useTeamStore((state) => state.setActiveTeam);

  const query = useTeams(activeOrgId, { pageSize: 100 }, { enabled: Boolean(activeOrgId) });
  const data = query.data;

  React.useEffect(() => {
    if (!query.isSuccess || !data) {

      return;
    }

    if (activeTeamId === null) {

      return;
    }

    const ids = data.results.map((t) => t.id);

    if (!ids.includes(activeTeamId)) {
      setActiveTeam(null);
    }
  }, [query.isSuccess, data, activeTeamId, setActiveTeam]);

  const teamItems: TeamListItem[] = React.useMemo(
    () => [
      { id: ALL_TEAMS_KEY, name: 'All teams' },
      ...(data?.results ?? []).map((t) => ({ id: t.id, name: t.name })),
    ],
    [data],
  );

  const selectedKey = activeTeamId ?? ALL_TEAMS_KEY;

  if (query.isLoading) {

    return (
      <div className='flex items-center gap-2 text-sm text-default-500'>
        <Loader2 className='w-4 h-4 animate-spin' />
        <span>Loading teams…</span>
      </div>
    );
  }

  return (
    <Select
      aria-label='Active team'
      classNames={{
        base: 'w-[200px]',
        trigger: 'h-9 min-h-9 bg-content2/60 border border-divider',
      }}
      items={teamItems}
      labelPlacement='outside'
      selectedKeys={new Set<string>([selectedKey])}
      startContent={<Users className='w-4 h-4 text-default-500' />}
      variant='bordered'
      onSelectionChange={(keys) => {
        const next = Array.from(keys)[0];

        if (next === ALL_TEAMS_KEY || next === undefined) {
          setActiveTeam(null);
        } else if (typeof next === 'string') {
          setActiveTeam(next);
        }
      }}
    >
      {(item) => (
        <SelectItem key={item.id}>{item.name}</SelectItem>
      )}
    </Select>
  );
}
