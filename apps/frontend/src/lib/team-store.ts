'use client';

import { persist } from 'zustand/middleware';
import { create } from 'zustand';

const TEAM_STORAGE_KEY = 'engram_active_team';

export type TeamStoreState = {
  activeTeamId: string | null;
  setActiveTeam: (teamId: string | null) => void;
  clearActiveTeam: () => void;
};

export const useTeamStore = create<TeamStoreState>()(
  persist(
    (set) => ({
      activeTeamId: null,
      setActiveTeam: (teamId) => {
        set({ activeTeamId: teamId });
      },
      clearActiveTeam: () => {
        set({ activeTeamId: null });
      },
    }),
    {
      name: TEAM_STORAGE_KEY,
      skipHydration: false,
    },
  ),
);
