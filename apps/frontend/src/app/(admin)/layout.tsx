'use client';

import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { Menu } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { OrgSwitcher } from '@/components/layout/org-switcher';
import { ProjectSwitcher } from '@/components/layout/project-switcher';
import { Sidebar } from '@/components/layout/sidebar';
import { TeamSwitcher } from '@/components/layout/team-switcher';
import { clearToken, fetchMe, getToken, hasCapability, logout, type MeResponse } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';

function FullPageLoader() {
  return (
    <div className='fixed inset-0 bg-background z-50 flex items-center justify-center'>
      <div className='w-10 h-10 border-3 border-content3 border-t-primary rounded-full animate-spin' />
    </div>
  );
}

function AccessGate() {
  return (
    <div className='fixed inset-0 bg-background z-50 flex items-center justify-center px-6 text-center'>
      <div>
        <h1 className='text-lg font-semibold text-foreground'>Sign in required</h1>
        <p className='mt-2 max-w-md text-sm text-default-500'>
          Redirecting to the login page.
        </p>
      </div>
    </div>
  );
}

export default function AdminShellLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = React.useState(false);
  const [hasToken, setHasToken] = React.useState<boolean | null>(null);
  const activeOrgId = useOrgStore((state) => state.activeOrgId);

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

  const handleLogout = React.useCallback(async () => {
    try {
      await logout();
    } finally {
      clearToken();
      setHasToken(false);
      router.replace('/login');
    }
  }, [router]);

  if (hasToken === null) {

    return <FullPageLoader />;
  }

  if (hasToken === false) {

    return <AccessGate />;
  }

  const profile = meQuery.data;

  return (
    <div className='min-h-screen bg-background text-foreground'>
      <Sidebar
        capabilities={profile?.capabilities ?? []}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onLogout={handleLogout}
      />

      <div className='lg:hidden fixed top-0 left-0 right-0 h-14 bg-content1 border-b border-divider flex items-center px-4 z-30'>
        <button
          className='p-2 -ml-2 text-default-600 hover:text-foreground'
          onClick={() => setSidebarOpen(true)}
          type='button'
        >
          <Menu className='w-5 h-5' />
        </button>
        <span className='ml-3 text-sm font-bold text-foreground'>Engram</span>
      </div>

      <header className='hidden lg:flex h-14 items-center justify-between px-8 border-b border-divider bg-content1/50 backdrop-blur'>
        <div className='flex items-center gap-3'>
          {profile && hasCapability(profile.capabilities, 'organizations:read') && (
            <OrgSwitcher orgId={activeOrgId} />
          )}
          {profile && hasCapability(profile.capabilities, 'projects:read') && (
            <ProjectSwitcher />
          )}
          {profile && hasCapability(profile.capabilities, 'teams:read') && (
            <TeamSwitcher />
          )}
        </div>
        {profile && (
          <div className={clsx('flex items-center gap-3 text-sm')}>
            <span className='text-default-500'>Signed in as</span>
            <span className='font-medium text-foreground'>{profile.username}</span>
          </div>
        )}
      </header>

      <main className='lg:ml-[240px] flex-1 overflow-y-auto bg-background pt-14 lg:pt-0'>
        <div className='p-6 lg:p-8 animate-fade-up'>{children}</div>
      </main>
    </div>
  );
}
