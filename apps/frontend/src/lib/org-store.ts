'use client';

import { persist } from 'zustand/middleware';
import { create } from 'zustand';

const ORG_STORAGE_KEY = 'engram_active_org';

export type OrgStoreState = {
  activeOrgId: string | null;
  setActiveOrg: (orgId: string | null) => void;
  clearActiveOrg: () => void;
};

export const useOrgStore = create<OrgStoreState>()(
  persist(
    (set) => ({
      activeOrgId: null,
      setActiveOrg: (orgId) => {
        set({ activeOrgId: orgId });
      },
      clearActiveOrg: () => {
        set({ activeOrgId: null });
      },
    }),
    {
      name: ORG_STORAGE_KEY,
      skipHydration: false,
    },
  ),
);
