'use client';

import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Globe, Menu } from 'lucide-react';
import { usePathname, useRouter } from 'next/navigation';
import * as React from 'react';

import { BrandMark } from '@/components/brand/brand-logo';
import { OrgSwitcher } from '@/components/layout/org-switcher';
import { ProjectSwitcher } from '@/components/layout/project-switcher';
import { Sidebar } from '@/components/layout/sidebar';
import { TeamSwitcher } from '@/components/layout/team-switcher';
import { InitialTile } from '@/components/ui/initial-tile';
import {
  clearToken,
  fetchMe,
  getToken,
  hasCapability,
  logout,
  type MeResponse,
} from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

function FullPageLoader() {
  return (
    <div className='fixed inset-0 z-50 flex items-center justify-center bg-background'>
      <div className='h-10 w-10 animate-spin rounded-full border-[3px] border-content3 border-t-primary' />
    </div>
  );
}

function AccessGate() {
  return (
    <div className='fixed inset-0 z-50 flex items-center justify-center bg-background px-6 text-center'>
      <div>
        <h1 className='text-lg font-semibold text-foreground'>Sign in required</h1>
        <p className='mt-2 max-w-md text-sm text-default-500'>
          Redirecting to the login page.
        </p>
      </div>
    </div>
  );
}

function deriveRoleLabel(capabilities: string[]): string {
  const has = (cap: string) => hasCapability(capabilities, cap);

  if (has('organizations:write') || capabilities.includes('*')) {
    return 'Owner';
  }

  if (has('members:admin') || has('api_keys:issue')) {
    return 'Admin';
  }

  return 'Member';
}

function Divider() {
  return <span className='text-default-400'>/</span>;
}

// Routes whose pages do not read the active project (they never call
// useProjectStore). Keep in sync when a page starts scoping by project.
const ORG_SCOPED_ROUTES = [
  '/api-keys',
  '/audit',
  '/health',
  '/members',
  '/memory-review',
  '/organizations',
  '/projects',
  '/roles',
  '/teams',
  '/workflow-runs',
];

function isOrgScopedRoute(pathname: string): boolean {
  return ORG_SCOPED_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`),
  );
}

function OrgWideBadge() {
  return (
    <span
      className='inline-flex items-center gap-1.5 rounded-[7px] bg-content2 px-2 py-0.5 text-[11px] font-medium text-default-500'
      title='This page is organization-wide; the project selector does not filter it.'
    >
      <Globe size={12} strokeWidth={1.9} />
      Org-wide
    </span>
  );
}

export default function AdminShellLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [sidebarOpen, setSidebarOpen] = React.useState(false);
  const [hasToken, setHasToken] = React.useState<boolean | null>(null);
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const orgScoped = isOrgScopedRoute(pathname);

  React.useEffect(() => {
    setHasToken(Boolean(getToken()));
  }, []);

  const meQuery = useQuery<MeResponse>({
    enabled: hasToken === true,
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
    retry: false,
  });

  React.useEffect(() => {
    if (hasToken === false) {
      router.replace('/login');
    }
  }, [hasToken, router]);

  const clearSession = React.useCallback(() => {
    clearToken();
    useOrgStore.getState().setActiveOrg(null);
    useProjectStore.getState().setActiveProject(null);
    useTeamStore.getState().setActiveTeam(null);
    setHasToken(false);
  }, []);

  React.useEffect(() => {
    if (
      meQuery.isError &&
      axios.isAxiosError(meQuery.error) &&
      (meQuery.error.response?.status === 401 ||
        meQuery.error.response?.status === 403)
    ) {
      clearSession();
    }
  }, [meQuery.isError, meQuery.error, clearSession]);

  const handleLogout = React.useCallback(async () => {
    try {
      await logout();
    } finally {
      clearSession();
      router.replace('/login');
    }
  }, [router, clearSession]);

  if (hasToken === null) {
    return <FullPageLoader />;
  }

  if (hasToken === false) {
    return <AccessGate />;
  }

  if (meQuery.isPending) {
    return <FullPageLoader />;
  }

  const profile = meQuery.data;
  const roleLabel = profile ? deriveRoleLabel(profile.capabilities) : '';

  return (
    <div className='min-h-screen bg-background text-foreground'>
      <Sidebar
        capabilities={profile?.capabilities ?? []}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onLogout={handleLogout}
      />

      {/* Mobile top bar */}
      <div className='fixed left-0 right-0 top-0 z-30 flex h-14 items-center border-b border-divider bg-content1 px-4 lg:hidden'>
        <button
          className='-ml-2 p-2 text-default-500 hover:text-foreground'
          onClick={() => setSidebarOpen(true)}
          type='button'
        >
          <Menu className='h-5 w-5' />
        </button>
        <span className='ml-3 flex items-center gap-2'>
          <BrandMark size={24} />
          <span className='text-sm font-semibold text-foreground'>Engram</span>
        </span>
      </div>

      <div className='lg:ml-[248px]'>
        {/* Desktop top bar */}
        <header className='top-bar-blur sticky top-0 z-30 hidden h-[60px] items-center justify-between border-b border-divider px-7 lg:flex'>
          <div className='flex min-w-0 items-center gap-2.5'>
            {profile && hasCapability(profile.capabilities, 'organizations:read') && (
              <OrgSwitcher orgId={activeOrgId} />
            )}
            {profile &&
              hasCapability(profile.capabilities, 'projects:read') && (
                <>
                  <Divider />
                  <ProjectSwitcher />
                  {orgScoped && <OrgWideBadge />}
                </>
              )}
            {profile && hasCapability(profile.capabilities, 'teams:read') && (
              <>
                <Divider />
                <TeamSwitcher />
              </>
            )}
          </div>

          <div className='flex items-center gap-3'>
            {profile && (
              <div className='flex items-center gap-2.5'>
                <div className='text-right leading-tight'>
                  <div className='text-[13px] font-semibold text-foreground'>
                    {profile.username}
                  </div>
                  <div className='text-[11px] text-default-400'>{roleLabel}</div>
                </div>
                <InitialTile name={profile.username} size={34} />
              </div>
            )}
          </div>
        </header>

        <main className='min-h-screen pt-14 lg:pt-0'>
          <div className='mx-auto max-w-[1140px] animate-fade-up px-5 pb-16 pt-7 sm:px-7'>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
