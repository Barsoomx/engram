'use client';

import clsx from 'clsx';
import {
  Activity,
  BadgeCheck,
  Boxes,
  Building2,
  CalendarClock,
  ClipboardList,
  Cpu,
  Database,
  FolderTree,
  Key,
  KeyRound,
  LayoutDashboard,
  LogOut,
  ScrollText,
  SearchCode,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  Webhook,
  Workflow,
  X,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import * as React from 'react';

import { BrandLockup } from '@/components/brand/brand-logo';
import { hasCapability } from '@/lib/auth';

export interface SidebarNavItem {
  href: string;
  label: string;
  icon: typeof LayoutDashboard;
  capability?: string;
  badge?: number;
}

interface SidebarNavGroup {
  title: string;
  items: SidebarNavItem[];
}

const NAV_GROUPS: SidebarNavGroup[] = [
  {
    title: 'Workspace',
    items: [
      { href: '/', label: 'Dashboard', icon: LayoutDashboard },
      { href: '/memories', label: 'Memories', icon: Database },
      { href: '/observations', label: 'Observations', icon: ScrollText },
      {
        href: '/memory-review',
        label: 'Memory Review',
        icon: ShieldCheck,
        capability: 'memories:review',
      },
      {
        href: '/projects',
        label: 'Projects',
        icon: FolderTree,
        capability: 'projects:read',
      },
      {
        href: '/search-debug',
        label: 'Search Debugger',
        icon: SearchCode,
        capability: 'memories:read',
      },
      {
        href: '/hook-debug',
        label: 'Hook Debugger',
        icon: Webhook,
        capability: 'observations:write',
      },
      {
        href: '/context-bundles',
        label: 'Context Bundles',
        icon: Boxes,
        capability: 'context:read',
      },
      {
        href: '/digests',
        label: 'Weekly Digest',
        icon: CalendarClock,
        capability: 'memories:read',
      },
      {
        href: '/workflow-runs',
        label: 'Workflow Runs',
        icon: Workflow,
        capability: 'memories:read',
      },
    ],
  },
  {
    title: 'Administration',
    items: [
      {
        href: '/secrets',
        label: 'Secrets',
        icon: KeyRound,
        capability: 'secrets:read',
      },
      {
        href: '/model-policies',
        label: 'Model Policies',
        icon: Cpu,
        capability: 'model_policy:read',
      },
      {
        href: '/model-setup',
        label: 'Model Setup',
        icon: SlidersHorizontal,
        capability: 'model_policy:read',
      },
      {
        href: '/organizations',
        label: 'Organizations',
        icon: Building2,
        capability: 'organizations:read',
      },
      { href: '/teams', label: 'Teams', icon: Users, capability: 'teams:read' },
      {
        href: '/members',
        label: 'Members',
        icon: Users,
        capability: 'members:read',
      },
      {
        href: '/roles',
        label: 'Roles',
        icon: BadgeCheck,
        capability: 'roles:read',
      },
      {
        href: '/api-keys',
        label: 'API Keys',
        icon: Key,
        capability: 'api_keys:read',
      },
      {
        href: '/audit',
        label: 'Audit log',
        icon: ClipboardList,
        capability: 'audit:read',
      },
      { href: '/settings', label: 'Settings', icon: Settings },
      { href: '/health', label: 'Health', icon: Activity },
    ],
  },
];

export interface SidebarProps {
  isOpen: boolean;
  onClose: () => void;
  onLogout: () => void;
  capabilities: string[];
}

function NavLink({
  item,
  active,
  onClose,
}: {
  item: SidebarNavItem;
  active: boolean;
  onClose: () => void;
}) {
  const Icon = item.icon;

  return (
    <Link
      className={clsx(
        'relative flex h-[38px] items-center gap-3 rounded-[9px] px-3 text-[13.5px] font-medium transition-colors duration-150 focus-visible:outline-hidden',
        active
          ? 'bg-[color:var(--accent-soft)] text-foreground before:absolute before:-left-3 before:top-[9px] before:bottom-[9px] before:w-[3px] before:rounded-r before:bg-primary'
          : 'text-default-500 hover:bg-content2/60 hover:text-foreground',
      )}
      href={item.href}
      onClick={onClose}
    >
      <Icon className='shrink-0' size={17} strokeWidth={1.8} />
      <span className='flex-1 truncate'>{item.label}</span>
      {typeof item.badge === 'number' && (
        <span className='rounded-full bg-content3 px-1.5 py-0.5 text-[10.5px] font-semibold tabular-nums text-default-600'>
          {item.badge}
        </span>
      )}
    </Link>
  );
}

export function Sidebar({ isOpen, onClose, onLogout, capabilities }: SidebarProps) {
  const pathname = usePathname();

  const isActive = (href: string) => {
    if (href === '/') {
      return pathname === '/';
    }

    return pathname.startsWith(href);
  };

  const groups = NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter(
      (item) => !item.capability || hasCapability(capabilities, item.capability),
    ),
  })).filter((group) => group.items.length > 0);

  return (
    <>
      {isOpen && (
        <div
          className='fixed inset-0 z-40 bg-black/60 lg:hidden'
          onClick={onClose}
        />
      )}

      <aside
        className={clsx(
          'fixed left-0 top-0 z-50 flex h-full w-[248px] flex-col border-r border-divider bg-[#0C0F14] transition-transform duration-200',
          'lg:translate-x-0',
          isOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0',
        )}
      >
        <div className='flex items-center justify-between px-5 pt-5 pb-4'>
          <BrandLockup size={32} />
          <button
            className='p-1 text-default-500 hover:text-foreground lg:hidden'
            onClick={onClose}
            type='button'
          >
            <X className='h-5 w-5' />
          </button>
        </div>

        <nav className='flex-1 space-y-5 overflow-y-auto px-4 py-4'>
          {groups.map((group) => (
            <div key={group.title} className='space-y-1'>
              <p className='px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>
                {group.title}
              </p>
              {group.items.map((item) => (
                <NavLink
                  key={item.href}
                  item={item}
                  active={isActive(item.href)}
                  onClose={onClose}
                />
              ))}
            </div>
          ))}
        </nav>

        <div className='border-t border-divider px-4 py-3'>
          <button
            className='flex h-[38px] w-full items-center gap-3 rounded-[9px] px-3 text-[13.5px] font-medium text-default-500 transition-colors hover:bg-content2/60 hover:text-foreground'
            onClick={onLogout}
            type='button'
          >
            <LogOut size={17} strokeWidth={1.8} className='shrink-0' />
            Sign out
          </button>
        </div>
      </aside>
    </>
  );
}
