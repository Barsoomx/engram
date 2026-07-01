'use client';

import { FolderTree, ListTree, Search } from 'lucide-react';
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
import { useProjects } from '@/hooks/use-projects';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { useSwitcherStore } from '@/lib/switcher-store';

export function ProjectSwitcher() {
  const router = useRouter();
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const activeProjectId = useProjectStore((state) => state.activeProjectId);
  const setActiveProject = useProjectStore((state) => state.setActiveProject);
  const open = useSwitcherStore((s) => s.openMenu === 'project');
  const toggle = useSwitcherStore((s) => s.toggle);
  const close = useSwitcherStore((s) => s.close);

  const query = useProjects(
    activeOrgId,
    { pageSize: 100 },
    { enabled: Boolean(activeOrgId) },
  );
  const data = query.data;
  const [search, setSearch] = React.useState('');

  React.useEffect(() => {
    if (!query.isSuccess || !data || data.results.length === 0) {
      return;
    }

    const ids = data.results.map((p) => p.id);

    if (!activeProjectId || !ids.includes(activeProjectId)) {
      setActiveProject(data.results[0].id);
    }
  }, [query.isSuccess, data, activeProjectId, setActiveProject]);

  React.useEffect(() => {
    if (!open) {
      setSearch('');
    }
  }, [open]);

  const projects = React.useMemo(() => data?.results ?? [], [data]);
  const activeProject =
    projects.find((p) => p.id === activeProjectId) ?? projects[0] ?? null;

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();

    if (!q) {
      return projects;
    }

    return projects.filter(
      (p) =>
        p.name.toLowerCase().includes(q) || p.slug.toLowerCase().includes(q),
    );
  }, [projects, search]);

  if (query.isLoading || !activeProject) {
    return (
      <div className='flex items-center gap-2 px-2 text-[13px] text-default-500'>
        <FolderTree className='h-4 w-4' />
        <span>{query.isLoading ? 'Loading…' : 'No projects'}</span>
      </div>
    );
  }

  return (
    <div className='relative'>
      {open && <SwitcherBackdrop onClose={close} />}

      <SwitcherTrigger active={open} onClick={() => toggle('project')}>
        <InitialTile
          name={activeProject.name}
          seed={activeProject.slug}
          size={18}
          variant='flat'
        />
        <span className='truncate font-mono text-[13px] font-semibold text-foreground'>
          {activeProject.slug}
        </span>
      </SwitcherTrigger>

      {open && (
        <DropdownPanel width={296}>
          <div className='p-1'>
            <div className='flex items-center gap-2 rounded-[9px] border border-divider bg-content2 px-2.5'>
              <Search size={15} strokeWidth={1.8} className='text-default-400' />
              <input
                autoFocus
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder='Search projects…'
                className='h-8 w-full bg-transparent text-[13px] text-foreground outline-hidden placeholder:text-default-400'
              />
            </div>
          </div>
          <DropdownEyebrow>Projects</DropdownEyebrow>
          <div className='max-h-[300px] space-y-0.5 overflow-y-auto'>
            {filtered.length === 0 ? (
              <p className='px-2.5 py-3 text-[12px] text-default-400'>
                No projects match “{search}”.
              </p>
            ) : (
              filtered.map((project) => (
                <MenuRow
                  key={project.id}
                  active={project.id === activeProject.id}
                  onClick={() => {
                    setActiveProject(project.id);
                    close();
                  }}
                >
                  <InitialTile
                    name={project.name}
                    seed={project.slug}
                    size={26}
                    variant='flat'
                  />
                  <div className='min-w-0 flex-1'>
                    <div className='truncate text-[13px] font-semibold text-foreground'>
                      {project.name}
                    </div>
                    <div className='truncate font-mono text-[11px] text-default-400'>
                      {project.slug}
                    </div>
                  </div>
                </MenuRow>
              ))
            )}
          </div>
          <DropdownDivider />
          <MenuActionRow
            icon={ListTree}
            label='View all projects'
            onClick={() => {
              close();
              router.push('/projects');
            }}
          />
        </DropdownPanel>
      )}
    </div>
  );
}
