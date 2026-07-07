'use client';

import { Input, Switch } from '@heroui/react';
import { useMutation } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, Key, Play, Shield } from 'lucide-react';
import * as React from 'react';

import { CopyButton } from '@/components/ui/copy-button';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { useProjects } from '@/hooks/use-projects';
import { useTeams } from '@/hooks/use-teams';
import { dryRunHook, genRequestId, type HookDryRunResult } from '@/lib/console-api';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {
      return data.detail;
    }

    const status = error.response?.status;

    if (status === 401) {
      return 'Authentication failed — check the API key, or your session may have expired.';
    }

    if (status === 403) {
      return 'The presented credential lacks the observations:write capability required for the hook handshake.';
    }
  }

  if (error instanceof Error) {
    return error.message;
  }

  return 'Handshake failed.';
}

function SectionHeading({
  icon,
  title,
  accent = 'default',
}: {
  icon: React.ReactNode;
  title: string;
  accent?: 'primary' | 'default';
}) {
  const tile = accent === 'primary' ? 'bg-primary-soft text-primary-300' : 'bg-content3 text-default-500';

  return (
    <div className='flex items-center gap-2.5'>
      <span className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] ${tile}`}>
        {icon}
      </span>
      <h3 className='text-[14.5px] font-semibold text-foreground'>{title}</h3>
    </div>
  );
}

function IdRow({ label, value }: { label: string; value: string }) {
  return (
    <div className='flex items-center gap-3 rounded-[10px] bg-content2/50 px-3 py-2'>
      <dt className='shrink-0 text-[12px] text-default-400'>{label}</dt>
      <dd className='ml-auto flex min-w-0 items-center gap-1.5'>
        <span className='truncate font-mono text-[12px] text-foreground'>{value}</span>
        <CopyButton value={value} />
      </dd>
    </div>
  );
}

function ResultSkeleton() {
  return (
    <div className='space-y-4'>
      <div className='surface-card h-[64px] animate-pulse bg-content1' />
      <div className='surface-card h-[136px] animate-pulse bg-content1' />
      <div className='h-[200px] animate-pulse rounded-[18px] border border-primary/20 bg-primary/[0.04]' />
    </div>
  );
}

function HookDryRunResults({ result }: { result: HookDryRunResult }) {
  const statusOk = result.status === 'ok';

  return (
    <div className='space-y-4'>
      <div className='surface-card flex items-center gap-4 p-[22px]'>
        <div className='flex flex-1 items-center gap-3'>
          <PulseDot color={statusOk ? '#3DD9AC' : '#666C77'} pulse={statusOk} size={8} />
          <span
            className={
              statusOk
                ? 'text-[14.5px] font-semibold text-success'
                : 'text-[14.5px] font-semibold text-default-500'
            }
          >
            {statusOk ? 'Handshake OK' : result.status}
          </span>
        </div>
        <span className='flex items-center gap-1.5 font-mono text-[11.5px] text-default-400'>
          {result.request_id}
          <CopyButton value={result.request_id} size={12} />
        </span>
      </div>

      <div className='surface-card space-y-4 p-[22px]'>
        <SectionHeading icon={<Key className='h-4 w-4' strokeWidth={1.8} />} title='Resolved actor' />
        <dl className='space-y-2'>
          <IdRow label='Type' value={result.resolved_actor.type} />
          <IdRow label='ID' value={result.resolved_actor.id} />
        </dl>
      </div>

      <div className='space-y-4 rounded-[18px] border border-primary/30 bg-primary/[0.04] p-[22px] shadow-primary-glow'>
        <SectionHeading icon={<Shield className='h-4 w-4' strokeWidth={1.8} />} title='Scope' accent='primary' />
        <dl className='space-y-2'>
          <IdRow label='Organization' value={result.scope.organization_id} />
        </dl>

        <div>
          <p className='mb-2 text-[12px] text-default-400'>Projects ({result.scope.project_ids.length})</p>
          {result.scope.project_ids.length > 0 ? (
            <div className='space-y-1'>
              {result.scope.project_ids.map((id) => (
                <div key={id} className='flex items-center gap-1.5'>
                  <span className='truncate font-mono text-[11.5px] text-foreground'>{id}</span>
                  <CopyButton value={id} size={12} />
                </div>
              ))}
            </div>
          ) : (
            <span className='font-mono text-[12px] text-default-400'>— (all projects)</span>
          )}
        </div>

        <div>
          <p className='mb-2 text-[12px] text-default-400'>Teams ({result.scope.team_ids.length})</p>
          {result.scope.team_ids.length > 0 ? (
            <div className='space-y-1'>
              {result.scope.team_ids.map((id) => (
                <div key={id} className='flex items-center gap-1.5'>
                  <span className='truncate font-mono text-[11.5px] text-foreground'>{id}</span>
                  <CopyButton value={id} size={12} />
                </div>
              ))}
            </div>
          ) : (
            <span className='font-mono text-[12px] text-default-400'>—</span>
          )}
        </div>

        <div>
          <p className='mb-2 text-[12px] text-default-400'>Capabilities ({result.scope.capabilities.length})</p>
          {result.scope.capabilities.length > 0 ? (
            <div className='flex flex-wrap gap-1.5'>
              {result.scope.capabilities.map((cap) => (
                <span
                  key={cap}
                  className='rounded-[7px] bg-primary-soft px-2 py-0.5 font-mono text-[11.5px] text-primary-300'
                >
                  {cap}
                </span>
              ))}
            </div>
          ) : (
            <span className='font-mono text-[12px] text-default-400'>—</span>
          )}
        </div>
      </div>

      <div className='surface-card flex items-center gap-3 p-[22px]'>
        <p className='text-[13.5px] font-semibold text-foreground'>Server health</p>
        <PulseDot color={result.server.health === 'ok' ? '#3DD9AC' : '#666C77'} pulse={result.server.health === 'ok'} size={7} />
        <span
          className={
            result.server.health === 'ok'
              ? 'text-[13px] font-medium text-success'
              : 'text-[13px] font-medium text-default-500'
          }
        >
          {result.server.health}
        </span>
      </div>
    </div>
  );
}

export default function HookDebugPage() {
  const activeOrgId = useOrgStore((s) => s.activeOrgId);
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const [apiKey, setApiKey] = React.useState('');
  const [narrowToProject, setNarrowToProject] = React.useState(false);

  const projectsQuery = useProjects(activeOrgId, { pageSize: 100 });
  const teamsQuery = useTeams(activeOrgId, { pageSize: 100 });

  const projectName = React.useMemo(
    () => projectsQuery.data?.results.find((p) => p.id === activeProjectId)?.name ?? activeProjectId,
    [projectsQuery.data, activeProjectId],
  );
  const teamName = React.useMemo(
    () => teamsQuery.data?.results.find((t) => t.id === activeTeamId)?.name ?? activeTeamId,
    [teamsQuery.data, activeTeamId],
  );

  const canNarrow = Boolean(activeProjectId);
  const narrowing = narrowToProject && canNarrow;

  const probe = useMutation<HookDryRunResult, unknown, void>({
    mutationFn: () =>
      dryRunHook(
        {
          project_id: narrowing ? activeProjectId : null,
          team_id: narrowing ? (activeTeamId ?? null) : null,
          request_id: genRequestId(),
        },
        apiKey || undefined,
      ),
  });

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Hook Debugger'
        subtitle='Resolve what an agent key — or your console session — is authorized to do.'
      />

      <div className='surface-card space-y-4 p-[22px]'>
        <Input
          label='Agent API key'
          labelPlacement='outside'
          placeholder='engram_sk_...'
          description='Leave blank to use your console session instead.'
          type='password'
          value={apiKey}
          onValueChange={setApiKey}
          isDisabled={probe.isPending}
        />

        <div className='flex items-start justify-between gap-4 rounded-[12px] border border-divider bg-content2/40 px-4 py-3'>
          <div className='min-w-0'>
            <p className='text-[13px] font-medium text-foreground'>Narrow to active project</p>
            <p className='mt-0.5 text-[12px] text-default-500'>
              {narrowing
                ? `Requesting scope for project ${projectName}${activeTeamId ? ` · team ${teamName}` : ''}.`
                : "Off: probes the credential's full reach across every authorized project."}
            </p>
            {!canNarrow && (
              <p className='mt-0.5 text-[11.5px] text-default-400'>Select a project in the switcher to enable narrowing.</p>
            )}
          </div>
          <Switch
            isSelected={narrowing}
            isDisabled={!canNarrow || probe.isPending}
            onValueChange={setNarrowToProject}
            size='sm'
          />
        </div>

        <div className='flex justify-end'>
          <PrimaryButton startContent={<Play className='h-4 w-4' />} onPress={() => probe.mutate()} isLoading={probe.isPending}>
            Run handshake
          </PrimaryButton>
        </div>
      </div>

      {probe.isError && (
        <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
          <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>{errorMessage(probe.error)}</p>
        </div>
      )}

      {probe.isPending && <ResultSkeleton />}

      {!probe.isPending && probe.data && <HookDryRunResults result={probe.data} />}
    </section>
  );
}
