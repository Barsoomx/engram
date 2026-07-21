'use client';

import { persist } from 'zustand/middleware';
import { create } from 'zustand';

import { shouldClearTeamOnProjectChange } from '@/lib/team-scope';
import { useTeamStore } from '@/lib/team-store';

const PROJECT_STORAGE_KEY = 'engram_active_project';

export type ProjectStoreState = {
  activeProjectId: string | null;
  setActiveProject: (projectId: string | null) => void;
  clearActiveProject: () => void;
};

export const useProjectStore = create<ProjectStoreState>()(
  persist(
    (set, get) => ({
      activeProjectId: null,
      setActiveProject: (projectId) => {
        const previous = get().activeProjectId;

        set({ activeProjectId: projectId });

        if (shouldClearTeamOnProjectChange(previous, projectId)) {
          useTeamStore.getState().clearActiveTeam();
        }
      },
      clearActiveProject: () => {
        set({ activeProjectId: null });
      },
    }),
    {
      name: PROJECT_STORAGE_KEY,
      skipHydration: false,
    },
  ),
);
