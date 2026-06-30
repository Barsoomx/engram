'use client';

import { Select, SelectItem } from '@heroui/react';
import { FolderTree, Loader2 } from 'lucide-react';
import * as React from 'react';

import { useProjects } from '@/hooks/use-projects';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';

export function ProjectSwitcher() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const activeProjectId = useProjectStore((state) => state.activeProjectId);
  const setActiveProject = useProjectStore((state) => state.setActiveProject);

  const query = useProjects(activeOrgId, { pageSize: 100 }, { enabled: Boolean(activeOrgId) });
  const data = query.data;

  React.useEffect(() => {
    if (!query.isSuccess || !data || data.results.length === 0) {

      return;
    }

    const ids = data.results.map((p) => p.id);

    if (!activeProjectId || !ids.includes(activeProjectId)) {
      setActiveProject(data.results[0].id);
    }
  }, [query.isSuccess, data, activeProjectId, setActiveProject]);

  const projects = data?.results ?? [];

  if (query.isLoading) {

    return (
      <div className='flex items-center gap-2 text-sm text-default-500'>
        <Loader2 className='w-4 h-4 animate-spin' />
        <span>Loading projects…</span>
      </div>
    );
  }

  if (query.isError || projects.length === 0) {

    return (
      <div className='flex items-center gap-2 text-sm text-default-500'>
        <FolderTree className='w-4 h-4' />
        <span>No projects</span>
      </div>
    );
  }

  return (
    <Select
      aria-label='Active project'
      classNames={{
        base: 'w-[220px]',
        trigger: 'h-9 min-h-9 bg-content2/60 border border-divider',
      }}
      items={projects}
      labelPlacement='outside'
      selectedKeys={activeProjectId ? new Set<string>([activeProjectId]) : new Set<string>()}
      startContent={<FolderTree className='w-4 h-4 text-default-500' />}
      variant='bordered'
      onSelectionChange={(keys) => {
        const next = Array.from(keys)[0];

        if (typeof next === 'string') {
          setActiveProject(next);
        }
      }}
    >
      {(project) => (
        <SelectItem key={project.id}>{project.name}</SelectItem>
      )}
    </Select>
  );
}
