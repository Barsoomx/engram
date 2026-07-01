'use client';

import { Button } from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import {
  Activity,
  AlertTriangle,
  Cpu,
  Database,
  LogOut,
  Server,
  User,
} from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { PageHeader } from '@/components/ui/page-header';
import { PulseDot } from '@/components/ui/pulse-dot';
import { useOrganizations } from '@/hooks/use-organizations';
import {
  useEmbeddingSettings,
  usePurgeOrganizationMemory,
  useRetrievalSettings,
  useUpdateRetrievalSettings,
} from '@/hooks/use-settings';
import { apiClient, clearToken, fetchMe, hasCapability, logout, type MeResponse } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import type { PurgeResult, RetrievalSettings } from '@/lib/settings-api';
import { useTeamStore } from '@/lib/team-store';

const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';

type HealthStatus = {
  ok: boolean;
  detail: string;
};

async function fetchHealth(): Promise<HealthStatus> {
  const client = apiClient();

  try {
    const response = await client.get('/-/healthz/', {
      headers: { Accept: 'text/plain, application/json' },
      transformResponse: (data) => data,
    });

    return {
      ok: response.status >= 200 && response.status < 300,
      detail: typeof response.data === 'string' ? response.data : JSON.stringify(response.data),
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);

    return {
      ok: false,
      detail: `Unreachable: ${message}`,
    };
  }
}

function Eyebrow({ children, tone = 'muted' }: { children: React.ReactNode; tone?: 'muted' | 'danger' }) {
  return (
    <span
      className={clsx(
        'text-[10.5px] font-semibold uppercase tracking-[0.12em]',
        tone === 'danger' ? 'text-danger' : 'text-default-400',
      )}
    >
      {children}
    </span>
  );
}

function Mono({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={clsx('break-all font-mono text-[12.5px] leading-relaxed text-default-700', className)}>
      {children}
    </span>
  );
}

function SettingsCard({
  icon,
  title,
  description,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className='surface-card flex flex-col p-[22px]'>
      <div className='flex items-start gap-3'>
        <div className='flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-content2 text-default-500'>
          {icon}
        </div>
        <div className='min-w-0 space-y-0.5'>
          <h2 className='text-[14.5px] font-semibold leading-tight text-foreground'>{title}</h2>
          {description && (
            <p className='text-[12.5px] leading-relaxed text-default-500'>{description}</p>
          )}
        </div>
      </div>
      <div className='mt-4'>{children}</div>
    </div>
  );
}

function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className='grid grid-cols-[110px_1fr] items-baseline gap-4 border-b border-divider py-2.5 last:border-b-0'>
      <dt className='pt-px'>
        <Eyebrow>{label}</Eyebrow>
      </dt>
      <dd className='min-w-0'>{children}</dd>
    </div>
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div className='space-y-1.5'>
      <Eyebrow>{label}</Eyebrow>
      <div className='flex h-10 items-center rounded-[10px] border border-divider bg-content2/60 px-3 text-[13px] text-default-700'>
        <span className='truncate'>{value}</span>
      </div>
    </div>
  );
}

function Toggle({
  label,
  description,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className='flex items-center justify-between gap-4 py-3'>
      <div className='min-w-0'>
        <p className='text-[13px] font-medium text-foreground'>{label}</p>
        <p className='text-[12px] leading-relaxed text-default-500'>{description}</p>
      </div>
      <button
        type='button'
        role='switch'
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx(
          'relative h-6 w-[42px] shrink-0 rounded-full transition-colors duration-150',
          checked ? 'bg-primary' : 'bg-content3',
          disabled && 'cursor-not-allowed opacity-60',
        )}
      >
        <span
          className={clsx(
            'absolute top-1/2 h-[18px] w-[18px] -translate-y-1/2 rounded-full bg-white shadow-xs transition-all duration-150',
            checked ? 'left-[21px]' : 'left-[3px]',
          )}
        />
      </button>
    </div>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const activeOrgId = useOrgStore((s) => s.activeOrgId);
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
    retry: false,
  });

  const healthQuery = useQuery<HealthStatus>({
    queryKey: ['health', 'healthz'],
    queryFn: fetchHealth,
    refetchInterval: 30000,
  });

  const orgsQuery = useOrganizations(activeOrgId);
  const embeddingQuery = useEmbeddingSettings(activeOrgId);
  const retrievalQuery = useRetrievalSettings(activeOrgId);
  const retrievalMutation = useUpdateRetrievalSettings(activeOrgId);
  const purgeMutation = usePurgeOrganizationMemory(activeOrgId);

  const [loggingOut, setLoggingOut] = React.useState(false);
  const [purgeOpen, setPurgeOpen] = React.useState(false);
  const [purgeConfirm, setPurgeConfirm] = React.useState('');
  const [purgeResult, setPurgeResult] = React.useState<PurgeResult['deleted'] | null>(null);

  const activeOrg = orgsQuery.data?.results.find((org) => org.id === activeOrgId);
  const orgSlug = activeOrg?.slug ?? '';

  const embedding = embeddingQuery.data;
  const retrieval = retrievalQuery.data;

  const handleRetrievalToggle = React.useCallback(
    (key: keyof RetrievalSettings, next: boolean) => {
      if (!retrieval) {
        return;
      }

      retrievalMutation.mutate({ ...retrieval, [key]: next });
    },
    [retrieval, retrievalMutation],
  );

  const handlePurge = React.useCallback(() => {
    purgeMutation.mutate(purgeConfirm, {
      onSuccess: (data) => {
        setPurgeResult(data.deleted);
        setPurgeOpen(false);
        setPurgeConfirm('');
      },
    });
  }, [purgeConfirm, purgeMutation]);

  const handleLogout = React.useCallback(async () => {
    setLoggingOut(true);

    try {
      await logout();
    } finally {
      clearToken();
      setLoggingOut(false);
      router.replace('/login');
    }
  }, [router]);

  const profile = meQuery.data;
  const health = healthQuery.data;
  const canPurge = hasCapability(profile?.capabilities ?? [], 'memories:admin');

  const healthState = healthQuery.isLoading ? 'checking' : health?.ok ? 'healthy' : 'unhealthy';
  const healthColor =
    healthState === 'healthy' ? '#3DD9AC' : healthState === 'unhealthy' ? '#FB6E72' : '#666C77';
  const healthLabel =
    healthState === 'healthy' ? 'Healthy' : healthState === 'unhealthy' ? 'Unhealthy' : 'Checking…';

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Settings'
        subtitle='Workspace configuration, backend status, and your session.'
      />

      <div className='grid gap-4 lg:grid-cols-2'>
        <SettingsCard
          icon={<User size={17} strokeWidth={1.8} />}
          title='Current user'
          description='Your authenticated identity and granted capabilities.'
        >
          {meQuery.isLoading && <p className='text-[13px] text-default-500'>Loading user…</p>}
          {meQuery.isError && (
            <div className='rounded-[10px] border border-danger/30 bg-danger/5 px-3 py-2.5 text-[12.5px] text-danger'>
              {meQuery.error instanceof Error ? meQuery.error.message : 'Failed to load user.'}
            </div>
          )}
          {profile && (
            <dl>
              <InfoRow label='User ID'>
                <Mono>{profile.user_id}</Mono>
              </InfoRow>
              <InfoRow label='Username'>
                <Mono>{profile.username}</Mono>
              </InfoRow>
              <InfoRow label='Identity'>
                <Mono>{profile.identity_id}</Mono>
              </InfoRow>
              <InfoRow label='Organization'>
                <Mono>{profile.organization_id}</Mono>
              </InfoRow>
              <InfoRow label='Capabilities'>
                {profile.capabilities.length > 0 ? (
                  <div className='flex flex-wrap gap-1.5'>
                    {profile.capabilities.map((capability) => (
                      <span
                        key={capability}
                        className='rounded-[7px] bg-primary-soft px-2 py-0.5 font-mono text-[11.5px] text-primary-300'
                      >
                        {capability}
                      </span>
                    ))}
                  </div>
                ) : (
                  <Mono className='text-default-400'>—</Mono>
                )}
              </InfoRow>
            </dl>
          )}
        </SettingsCard>

        <SettingsCard
          icon={<Activity size={17} strokeWidth={1.8} />}
          title='Backend health'
          description='Live status of the Engram API.'
        >
          <div className='flex items-center gap-2.5'>
            <PulseDot color={healthColor} pulse={healthState === 'healthy'} />
            <span className='text-[13px] font-medium' style={{ color: healthColor }}>
              {healthLabel}
            </span>
          </div>
          <pre className='mt-4 max-h-40 overflow-auto rounded-[10px] border border-divider bg-content2/60 p-3 font-mono text-[11.5px] leading-relaxed text-default-500'>
            {healthQuery.isLoading ? 'loading…' : health?.detail || '(empty)'}
          </pre>
        </SettingsCard>

        <SettingsCard
          icon={<Server size={17} strokeWidth={1.8} />}
          title='Environment'
          description='Active workspace context for API requests.'
        >
          <dl>
            <InfoRow label='API URL'>
              <Mono className='text-primary-300'>{API_URL}</Mono>
            </InfoRow>
            <InfoRow label='Project'>
              {activeProjectId ? (
                <Mono>{activeProjectId}</Mono>
              ) : (
                <span className='font-mono text-[12.5px] text-default-400'>not set</span>
              )}
            </InfoRow>
            <InfoRow label='Team'>
              {activeTeamId ? (
                <Mono>{activeTeamId}</Mono>
              ) : (
                <span className='font-mono text-[12.5px] text-default-400'>not set</span>
              )}
            </InfoRow>
          </dl>
        </SettingsCard>

        <SettingsCard
          icon={<Cpu size={17} strokeWidth={1.8} />}
          title='Memory model'
          description='Embedding provider used to index and retrieve memories.'
        >
          <div className='grid gap-3 sm:grid-cols-2'>
            <ReadOnlyField
              label='Provider'
              value={embeddingQuery.isLoading ? '…' : embedding?.provider ?? 'Not configured'}
            />
            <ReadOnlyField
              label='Model'
              value={embeddingQuery.isLoading ? '…' : embedding?.model ?? 'Not configured'}
            />
          </div>
          <p className='mt-3 text-[11.5px] text-default-400'>
            Read-only · configure providers in Model policies.
          </p>
        </SettingsCard>

        <SettingsCard
          icon={<Database size={17} strokeWidth={1.8} />}
          title='Retrieval'
          description='How memories are ranked before injection.'
        >
          <div className='divide-y divide-divider'>
            <Toggle
              label='Hybrid retrieval'
              description='Combine vector similarity with keyword search.'
              checked={retrieval?.hybrid_retrieval_enabled ?? false}
              onChange={(next) => handleRetrievalToggle('hybrid_retrieval_enabled', next)}
              disabled={!retrieval || retrievalMutation.isPending}
            />
            <Toggle
              label='Require provenance'
              description='Only inject memories with a verified source.'
              checked={retrieval?.require_provenance ?? false}
              onChange={(next) => handleRetrievalToggle('require_provenance', next)}
              disabled={!retrieval || retrievalMutation.isPending}
            />
          </div>
          <p className='mt-3 text-[11.5px] text-default-400'>
            {retrievalQuery.isLoading
              ? 'Loading…'
              : retrievalQuery.isError
                ? 'Unavailable · requires organization admin.'
                : retrievalMutation.isPending
                  ? 'Saving…'
                  : 'Changes are saved automatically.'}
          </p>
        </SettingsCard>

        <SettingsCard
          icon={<LogOut size={17} strokeWidth={1.8} />}
          title='Session'
          description='Sign out of the Engram console on this device.'
        >
          <Button
            color='danger'
            variant='flat'
            disableRipple
            isDisabled={loggingOut}
            isLoading={loggingOut}
            onPress={handleLogout}
            className='h-10 rounded-[11px] px-4 text-[13.5px] font-medium'
          >
            {loggingOut ? 'Signing out…' : 'Sign out'}
          </Button>
        </SettingsCard>
      </div>

      {canPurge && (
        <div className='rounded-[16px] border border-danger/25 bg-danger/[0.04] p-[22px]'>
        <div className='flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between'>
          <div className='flex items-start gap-3'>
            <div className='flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-danger/10 text-danger'>
              <AlertTriangle size={17} strokeWidth={1.8} />
            </div>
            <div className='min-w-0 space-y-0.5'>
              <Eyebrow tone='danger'>Danger zone</Eyebrow>
              <h2 className='text-[14.5px] font-semibold leading-tight text-foreground'>
                Purge organization memory
              </h2>
              <p className='text-[12.5px] leading-relaxed text-default-500'>
                Permanently delete every captured memory for this organization. This cannot be undone.
              </p>
            </div>
          </div>
          {!purgeOpen && (
            <button
              type='button'
              onClick={() => {
                setPurgeResult(null);
                setPurgeOpen(true);
              }}
              disabled={!orgSlug}
              className='inline-flex h-10 shrink-0 items-center justify-center rounded-[11px] border border-danger/40 px-4 text-[13.5px] font-medium text-danger transition-colors hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-50'
            >
              Purge…
            </button>
          )}
        </div>

        {purgeOpen && (
          <div className='mt-4 space-y-3 rounded-[12px] border border-danger/30 bg-danger/[0.05] p-4'>
            <p className='text-[12.5px] text-default-500'>
              Type <span className='font-mono text-danger'>{orgSlug}</span> to confirm.
            </p>
            <input
              autoFocus
              value={purgeConfirm}
              onChange={(event) => setPurgeConfirm(event.target.value)}
              placeholder={orgSlug}
              className='h-10 w-full rounded-[10px] border border-divider-strong bg-content2 px-3 font-mono text-[13px] text-foreground outline-hidden transition-colors focus:border-danger/60'
            />
            {purgeMutation.isError && (
              <p className='text-[12px] text-danger'>
                {purgeMutation.error instanceof Error ? purgeMutation.error.message : 'Purge failed.'}
              </p>
            )}
            <div className='flex items-center gap-2'>
              <button
                type='button'
                onClick={handlePurge}
                disabled={purgeConfirm !== orgSlug || !orgSlug || purgeMutation.isPending}
                className='inline-flex h-10 items-center justify-center rounded-[11px] bg-danger px-4 text-[13.5px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50'
              >
                {purgeMutation.isPending ? 'Purging…' : 'Purge permanently'}
              </button>
              <button
                type='button'
                onClick={() => {
                  setPurgeOpen(false);
                  setPurgeConfirm('');
                }}
                disabled={purgeMutation.isPending}
                className='inline-flex h-10 items-center justify-center rounded-[11px] border border-divider px-4 text-[13.5px] font-medium text-default-600 transition-colors hover:bg-content2'
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {purgeResult && (
          <div className='mt-4 rounded-[12px] border border-success/30 bg-success/[0.06] p-3 text-[12.5px] text-success'>
            Purged {purgeResult.memories.toLocaleString()} memories,{' '}
            {purgeResult.memory_candidates.toLocaleString()} candidates, and{' '}
            {purgeResult.retrieval_documents.toLocaleString()} retrieval documents.
          </div>
        )}
        </div>
      )}
    </section>
  );
}
