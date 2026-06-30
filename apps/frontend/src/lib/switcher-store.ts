'use client';

import { create } from 'zustand';

export type SwitcherMenu = 'org' | 'project' | 'team' | null;

export interface SwitcherStoreState {
  openMenu: SwitcherMenu;
  toggle: (menu: Exclude<SwitcherMenu, null>) => void;
  open: (menu: Exclude<SwitcherMenu, null>) => void;
  close: () => void;
}

export const useSwitcherStore = create<SwitcherStoreState>((set) => ({
  openMenu: null,
  toggle: (menu) =>
    set((state) => ({ openMenu: state.openMenu === menu ? null : menu })),
  open: (menu) => set({ openMenu: menu }),
  close: () => set({ openMenu: null }),
}));
