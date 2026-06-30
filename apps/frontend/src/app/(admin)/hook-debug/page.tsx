'use client';

import { Input } from '@heroui/react';
import { useMutation, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, Key, Play, Shield, Target } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { fetchMe, type MeResponse } from '@/lib/auth';
import {
  dryRunHook,
  genRequestId,
  type HookDryRunResult,
} from '@/lib/console-api';
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
      return 'The API key is invalid or expired.';
    }

    if (status === 403) {
      return 'The API key does not have the observations:write capability required for the hook handshake.';
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
  const tile =
    accent === 'primary'
      ? 'bg-primary-soft text-primary-300'
      : 'bg-content3 text-default-500';

  return (
    <div className='flex items-center gap-2.5'>
      <span
        className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] ${tile}`}
      >
        {icon}
      </span>
      <h3 className='text-[14.5px] font-semibold text-foreground'>{title}</h3>
    </div>
  );
}

function ResultSkeleton() {
  return (
    <div className='space-y-4'>
      <div className='surface-card h-[64px] animate-pulse bg-content1' />
      <div className='surface-card h-[136px] animate-pulse bg-content1' />
      <div className='h-[200px] animate-pulse rounded-[18px] border border-primary/20 bg-primary/[0.04]' />
      <div className='surface-card h-[56px] animate-pulse bg-content1' />
    </div>
  );
}

function HookDryRunResults({ result }: { result: HookDryRunResult }) {
  const statusOk = result.status === 'ok';

  return (
    <div className='space-y-4'>
      <div className='surface-card flex items-center gap-4 p-[22px]'>
        <div className='flex flex-1 items-center gap-3'>
          <PulseDot
            color={statusOk ? '#3DD9AC' : '#666C77'}
            pulse={statusOk}
            size={8}
          />
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
        <span className='font-mono text-[11.5px] text-default-400'>
          {result.request_id}
        </span>
      </div>

      <div className='surface-card space-y-4 p-[22px]'>
        <SectionHeading
          icon={<Key className='h-4 w-4' strokeWidth={1.8} />}
          title='Resolved actor'
        />
        <dl className='space-y-2'>
          <div className='flex items-center gap-3 rounded-[10px] bg-content2/50 px-3 py-2'>
            <dt className='shrink-0 text-[12px] text-default-400'>Type</dt>
            <dd className='ml-auto font-mono text-[12px] text-foreground'>
              {result.resolved_actor.type}
            </dd>
          </div>
          <div className='flex items-center gap-3 rounded-[10px] bg-content2/50 px-3 py-2'>
            <dt className='shrink-0 text-[12px] text-default-400'>ID</dt>
            <dd className='ml-auto truncate font-mono text-[12px] text-foreground'>
              {result.resolved_actor.id}
            </dd>
          </div>
        </dl>
      </div>

      <div className='space-y-4 rounded-[18px] border border-primary/30 bg-primary/[0.04] p-[22px] shadow-primary-glow'>
        <SectionHeading
          icon={<Shield className='h-4 w-4' strokeWidth={1.8} />}
          title='Scope'
          accent='primary'
        />
        <dl className='space-y-2'>
          <div className='flex items-center gap-3 rounded-[10px] bg-content2/50 px-3 py-2'>
            <dt className='shrink-0 text-[12px] text-default-400'>
              Organization
            </dt>
            <dd className='ml-auto truncate font-mono text-[12px] text-foreground'>
              {result.scope.organization_id}
            </dd>
          </div>
          <div className='flex items-start gap-3 rounded-[10px] bg-content2/50 px-3 py-2'>
            <dt className='mt-0.5 shrink-0 text-[12px] text-default-400'>
              Projects ({result.scope.project_ids.length})
            </dt>
            <dd className='ml-auto text-right'>
              {result.scope.project_ids.length > 0 ? (
                <ul className='space-y-1'>
                  {result.scope.project_ids.map((id) => (
                    <li key={id} className='font-mono text-[11.5px] text-foreground'>
                      {id}
                    </li>
                  ))}
                </ul>
              ) : (
                <span className='font-mono text-[12px] text-default-400'>—</span>
              )}
            </dd>
          </div>
          <div className='flex items-start gap-3 rounded-[10px] bg-content2/50 px-3 py-2'>
            <dt className='mt-0.5 shrink-0 text-[12px] text-default-400'>
              Teams ({result.scope.team_ids.length})
            </dt>
            <dd className='ml-auto text-right'>
              {result.scope.team_ids.length > 0 ? (
                <ul className='space-y-1'>
                  {result.scope.team_ids.map((id) => (
                    <li key={id} className='font-mono text-[11.5px] text-foreground'>
                      {id}
                    </li>
                  ))}
                </ul>
              ) : (
                <span className='font-mono text-[12px] text-default-400'>—</span>
              )}
            </dd>
          </div>
        </dl>

        <div>
          <p className='mb-2 text-[12px] text-default-400'>
            Capabilities ({result.scope.capabilities.length})
          </p>
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
        <p className='text-[13.5px] font-semibold text-foreground'>
          Server health
        </p>
        <PulseDot
          color={result.server.health === 'ok' ? '#3DD9AC' : '#666C77'}
          pulse={result.server.health === 'ok'}
          size={7}
        />
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
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });
  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [apiKey, setApiKey] = React.useState('');
  const [agentRuntime, setAgentRuntime] = React.useState('claude_code');
  const [agentVersion, setAgentVersion] = React.useState('');

  const probe = useMutation<HookDryRunResult, unknown, void>({
    mutationFn: () =>
      dryRunHook(apiKey, {
        project_id: activeProjectId!,
        team_id: activeTeamId ?? null,
        agent_runtime: agentRuntime,
        agent_version: agentVersion || undefined,
        request_id: genRequestId(),
      }),
  });

  function handleRun() {
    if (!activeProjectId || !apiKey) {
      return;
    }

    probe.mutate();
  }

  return (
    <CapabilityGate capabilities={capabilities} required='api_keys:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Hook Debugger'
          subtitle='Resolve what an agent API key is authorized to do (Bearer handshake).'
        />

        {!activeProjectId ? (
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to run a hook handshake.'
            icon={<Target className='h-6 w-6' />}
          />
        ) : (
          <>
            <div className='surface-card space-y-4 p-[22px]'>
              <Input
                label='Agent API key'
                labelPlacement='outside'
                placeholder='engram_sk_...'
                type='password'
                value={apiKey}
                onValueChange={setApiKey}
                isDisabled={probe.isPending}
              />
              <div className='grid gap-4 sm:grid-cols-2'>
                <Input
                  label='Agent runtime'
                  labelPlacement='outside'
                  placeholder='claude_code'
                  value={agentRuntime}
                  onValueChange={setAgentRuntime}
                  isDisabled={probe.isPending}
                />
                <Input
                  label='Agent version'
                  labelPlacement='outside'
                  placeholder='Optional'
                  description='Leave blank to omit.'
                  value={agentVersion}
                  onValueChange={setAgentVersion}
                  isDisabled={probe.isPending}
                />
              </div>
              <div className='flex justify-end'>
                <PrimaryButton
                  startContent={<Play className='h-4 w-4' />}
                  onPress={handleRun}
                  isLoading={probe.isPending}
                  isDisabled={!apiKey}
                >
                  Run handshake
                </PrimaryButton>
              </div>
            </div>

            {probe.isError && (
              <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
                <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
                <p className='text-[13px] leading-relaxed text-danger'>
                  {errorMessage(probe.error)}
                </p>
              </div>
            )}

            {probe.isPending && <ResultSkeleton />}

            {!probe.isPending && probe.data && (
              <HookDryRunResults result={probe.data} />
            )}
          </>
        )}
      </section>
    </CapabilityGate>
  );
}
