'use client';

import clsx from 'clsx';
import {
  Activity,
  BadgeCheck,
  Building2,
  ClipboardList,
  Database,
  FolderTree,
  Key,
  LayoutDashboard,
  LogOut,
  ScrollText,
  ShieldCheck,
  Users,
  X,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import * as React from 'react';

import { hasCapability, logout } from '@/lib/auth';

export interface SidebarNavItem {
  href: string;
  label: string;
  icon: typeof LayoutDashboard;
  capability?: string;
}

const NAV_ITEMS: SidebarNavItem[] = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/memories', label: 'Memories', icon: Database },
  { href: '/observations', label: 'Observations', icon: ScrollText },
  { href: '/memory-review', label: 'Memory Review', icon: ShieldCheck, capability: 'memories:review' },
  { href: '/organizations', label: 'Organizations', icon: Building2, capability: 'organizations:read' },
  { href: '/teams', label: 'Teams', icon: Users, capability: 'teams:read' },
  { href: '/projects', label: 'Projects', icon: FolderTree, capability: 'projects:read' },
  { href: '/members', label: 'Members', icon: Users, capability: 'members:read' },
  { href: '/roles', label: 'Roles', icon: BadgeCheck, capability: 'roles:read' },
  { href: '/api-keys', label: 'API Keys', icon: Key, capability: 'api_keys:read' },
  { href: '/audit', label: 'Audit', icon: ClipboardList, capability: 'audit:read' },
  { href: '/health', label: 'Health', icon: Activity },
];

export interface SidebarProps {
  isOpen: boolean;
  onClose: () => void;
  onLogout: () => void;
  capabilities: string[];
}

export function Sidebar({ isOpen, onClose, onLogout, capabilities }: SidebarProps) {
  const pathname = usePathname();

  const visibleItems = NAV_ITEMS.filter((item) => {
    if (!item.capability) {

      return true;
    }

    return hasCapability(capabilities, item.capability);
  });

  const isActive = (href: string) => {
    if (href === '/') {

      return pathname === '/';
    }

    return pathname.startsWith(href);
  };

  return (
    <>
      {isOpen && (
        <div
          className='fixed inset-0 bg-black/60 z-40 lg:hidden'
          onClick={onClose}
        />
      )}

      <aside
        className={clsx(
          'fixed left-0 top-0 h-full w-[240px] bg-content1 border-r border-divider flex flex-col z-50 transition-transform duration-200',
          'lg:translate-x-0',
          isOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0',
        )}
      >
        <div className='px-6 py-5 border-b border-divider flex items-center justify-between'>
          <div>
            <h1 className='text-lg font-semibold tracking-tight text-foreground'>
              Engram
            </h1>
            <p className='text-xs uppercase tracking-wider text-default-500 -mt-0.5'>
              Admin
            </p>
          </div>
          <button
            className='lg:hidden p-1 text-default-600 hover:text-foreground'
            onClick={onClose}
            type='button'
          >
            <X className='w-5 h-5' />
          </button>
        </div>

        <nav className='flex-1 px-3 py-4 space-y-1'>
          {visibleItems.map((item) => {
            const Icon = item.icon;
            const active = isActive(item.href);

            return (
              <Link
                key={item.href}
                className={clsx(
                  active
                    ? 'relative flex items-center gap-3 h-10 px-3 rounded-medium text-sm bg-content2 text-foreground transition-colors duration-150 focus-visible:outline-none before:absolute before:left-0 before:top-2 before:bottom-2 before:w-0.5 before:bg-foreground before:rounded-r'
                    : 'flex items-center gap-3 h-10 px-3 rounded-medium text-sm text-default-700 hover:text-foreground hover:bg-content2/50 transition-colors duration-150 focus-visible:outline-none',
                )}
                href={item.href}
                onClick={onClose}
              >
                <Icon className='w-5 h-5' />
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className='px-3 py-4 border-t border-divider'>
          <button
            className='flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-default-600 hover:bg-content2 hover:text-foreground transition-colors w-full'
            onClick={onLogout}
            type='button'
          >
            <LogOut className='w-5 h-5' />
            Sign out
          </button>
        </div>
      </aside>
    </>
  );
}
