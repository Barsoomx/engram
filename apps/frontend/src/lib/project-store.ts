'use client';

import { persist } from 'zustand/middleware';
import { create } from 'zustand';

const PROJECT_STORAGE_KEY = 'engram_active_project';

export type ProjectStoreState = {
  activeProjectId: string | null;
  setActiveProject: (projectId: string | null) => void;
  clearActiveProject: () => void;
};

export const useProjectStore = create<ProjectStoreState>()(
  persist(
    (set) => ({
      activeProjectId: null,
      setActiveProject: (projectId) => {
        set({ activeProjectId: projectId });
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
