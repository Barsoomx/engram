'use client';

import { Input, Textarea } from '@heroui/react';
import { useMutation, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Ban, Filter, Layers, ListTree, Play, Sparkles, Target } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { ConfidenceTrack } from '@/components/ui/confidence-track';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { KindBadge } from '@/components/ui/kind-badge';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { fetchMe, type MeResponse } from '@/lib/auth';
import { replaySearchDebug, type SearchDebugRequest, type SearchDebugResult } from '@/lib/console-api';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const EXCLUSION_REASON_LABELS: Record<string, string> = {
  not_approved: 'Not approved',
  stale: 'Stale',
  refuted: 'Refuted',
  team_not_in_scope: 'Team not in scope',
  visibility_not_injectable: 'Visibility not injectable',
  below_relevance: 'Below relevance threshold',
  token_budget: 'Token budget exceeded',
};

function humanizeReason(value: string): string {
  if (EXCLUSION_REASON_LABELS[value]) {
    return EXCLUSION_REASON_LABELS[value];
  }

  const spaced = value.replace(/[_.]/g, ' ').trim();

  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : value;
}

function confidencePct(value: string | null): number | null {
  if (value === null) {
    return null;
  }

  const parsed = Number(value);

  if (!Number.isFinite(parsed)) {
    return null;
  }

  const pct = parsed <= 1 ? parsed * 100 : parsed;

  return Math.max(0, Math.min(100, Math.round(pct)));
}

function splitTokens(value: string): string[] {
  return value
    .split(/[\n,]+/)
    .map((token) => token.trim())
    .filter((token) => token.length > 0);
}

function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

function scorePct(value: number, max: number): number {
  if (max <= 0) {
    return 0;
  }

  return Math.max(0, Math.min(100, Math.round((value / max) * 100)));
}

function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {
      return data.detail;
    }
  }

  if (error instanceof Error) {
    return error.message;
  }

  return 'Replay failed.';
}

function MemoryLink({ id }: { id: string }) {
  if (!id) {
    return <span className='font-mono text-[11.5px] text-default-400'>—</span>;
  }

  return (
    <Link href={`/memories/${id}`} className='block truncate font-mono text-[11.5px] text-primary-300 hover:underline' title={id}>
      {id}
    </Link>
  );
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className='surface-card p-[18px]'>
      <p className='text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>{label}</p>
      <p className='tnum mt-2 text-[27px] font-semibold leading-none tracking-[-0.02em] text-foreground'>{value}</p>
    </div>
  );
}

function RankingCard({ label, enabled }: { label: string; enabled: boolean }) {
  return (
    <div className='surface-card p-[18px]'>
      <p className='text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>{label}</p>
      <div className='mt-2 inline-flex items-center gap-2'>
        <PulseDot color={enabled ? '#3DD9AC' : '#666C77'} pulse={enabled} size={8} />
        <span className={enabled ? 'text-[14.5px] font-semibold text-success' : 'text-[14.5px] font-semibold text-default-500'}>
          {enabled ? 'Enabled' : 'Disabled'}
        </span>
      </div>
    </div>
  );
}

function SectionHeading({
  icon,
  title,
  count,
  accent,
}: {
  icon: React.ReactNode;
  title: string;
  count: number;
  accent: 'primary' | 'default' | 'danger';
}) {
  const tile =
    accent === 'primary'
      ? 'bg-primary-soft text-primary-300'
      : accent === 'danger'
        ? 'bg-danger/10 text-danger'
        : 'bg-content3 text-default-500';

  return (
    <div className='flex items-center justify-between gap-3'>
      <div className='flex items-center gap-2.5'>
        <span className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] ${tile}`}>{icon}</span>
        <h3 className='text-[14.5px] font-semibold text-foreground'>{title}</h3>
      </div>
      <span className='tnum rounded-[7px] bg-content3 px-2 py-0.5 font-mono text-[11px] text-default-400'>{count}</span>
    </div>
  );
}

function ScoreReadout({ value, max }: { value: number; max: number }) {
  return (
    <div className='flex shrink-0 items-center gap-2.5'>
      <span className='tnum font-mono text-[12px] text-default-400'>{formatScore(value)}</span>
      <ConfidenceTrack value={scorePct(value, max)} width={48} height={5} />
    </div>
  );
}

function ConfidenceReadout({ value }: { value: string | null }) {
  const pct = confidencePct(value);

  if (pct === null) {
    return null;
  }

  return <span className='tnum shrink-0 font-mono text-[11px] text-default-400'>{pct}% conf</span>;
}

function SearchDebugResults({ result }: { result: SearchDebugResult }) {
  const [scopeOpen, setScopeOpen] = React.useState(false);

  const exactMatches = React.useMemo(() => [...result.exact_matches].sort((a, b) => b.score - a.score), [result.exact_matches]);
  const exactMax = React.useMemo(() => exactMatches.reduce((max, item) => Math.max(max, item.score), 0), [exactMatches]);

  const semanticCandidates = React.useMemo(
    () => [...result.semantic_candidates].sort((a, b) => b.score - a.score),
    [result.semantic_candidates],
  );
  const semanticMax = React.useMemo(() => semanticCandidates.reduce((max, item) => Math.max(max, item.score), 0), [semanticCandidates]);

  const lexicalCandidates = React.useMemo(
    () => [...result.lexical_candidates].sort((a, b) => b.score - a.score),
    [result.lexical_candidates],
  );
  const lexicalMax = React.useMemo(() => lexicalCandidates.reduce((max, item) => Math.max(max, item.score), 0), [lexicalCandidates]);

  const excludedByReason = React.useMemo(() => {
    const groups = new Map<string, SearchDebugResult['excluded']>();

    for (const item of result.excluded) {
      const existing = groups.get(item.reason) ?? [];
      existing.push(item);
      groups.set(item.reason, existing);
    }

    return Array.from(groups.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [result.excluded]);

  return (
    <div className='space-y-5'>
      <div className='grid gap-3 sm:grid-cols-2 lg:grid-cols-5'>
        <StatCard label='Candidate universe' value={result.candidate_universe_count} />
        <StatCard label='Exact matches' value={result.exact_matches.length} />
        <StatCard label='Packed context' value={result.packed_context.length} />
        <RankingCard label='Semantic ranking' enabled={result.semantic_enabled} />
        <RankingCard label='Lexical ranking' enabled={result.lexical_enabled} />
      </div>

      <div className='rounded-[18px] border border-primary/30 bg-primary/[0.04] p-[22px] shadow-primary-glow'>
        <SectionHeading icon={<Layers className='h-4 w-4' strokeWidth={1.8} />} title='Packed context' count={result.packed_context.length} accent='primary' />
        <p className='mt-1.5 text-[12px] text-default-500'>Final memories injected into the bundle, in order.</p>
        {result.packed_context.length > 0 ? (
          <ol className='mt-4 space-y-2'>
            {result.packed_context.map((item, index) => (
              <li key={item.memory_id} className='flex items-center gap-3 rounded-[12px] border border-primary/20 bg-primary/[0.05] px-4 py-3'>
                <span className='tnum inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-soft font-mono text-[12px] font-semibold text-primary-300'>
                  {index + 1}
                </span>
                <div className='min-w-0 flex-1'>
                  <div className='flex items-center gap-2'>
                    <KindBadge kind={item.kind} />
                    <p className='min-w-0 truncate text-[13.5px] font-medium text-foreground'>{item.title || '(untitled)'}</p>
                  </div>
                  <MemoryLink id={item.memory_id} />
                </div>
                <ConfidenceReadout value={item.confidence} />
              </li>
            ))}
          </ol>
        ) : (
          <p className='mt-4 text-[13px] text-default-500'>No memories were packed into the context bundle.</p>
        )}
      </div>

      <div className='surface-card p-[22px]'>
        <SectionHeading icon={<Target className='h-4 w-4' strokeWidth={1.8} />} title='Exact matches' count={exactMatches.length} accent='default' />
        {exactMatches.length > 0 ? (
          <div className='mt-3'>
            {exactMatches.map((item) => (
              <div key={item.memory_id} className='flex items-center gap-4 border-b border-divider py-3 last:border-b-0'>
                <div className='flex min-w-0 flex-1 items-center gap-2'>
                  <KindBadge kind={item.kind} />
                  <div className='min-w-0'>
                    <p className='truncate text-[13.5px] font-medium text-foreground'>{item.title || '(untitled)'}</p>
                    <MemoryLink id={item.memory_id} />
                  </div>
                </div>
                <ConfidenceReadout value={item.confidence} />
                <span className='shrink-0 rounded-[7px] bg-content3 px-2 py-1 font-mono text-[11px] text-default-500' title={item.matched_on}>
                  {humanizeReason(item.matched_on)}
                </span>
                <ScoreReadout value={item.score} max={exactMax} />
              </div>
            ))}
          </div>
        ) : (
          <p className='mt-3 text-[13px] text-default-500'>No exact matches.</p>
        )}
      </div>

      {result.semantic_enabled && (
        <div className='surface-card p-[22px]'>
          <SectionHeading icon={<Sparkles className='h-4 w-4' strokeWidth={1.8} />} title='Semantic candidates' count={semanticCandidates.length} accent='default' />
          {semanticCandidates.length > 0 ? (
            <div className='mt-3'>
              {semanticCandidates.map((item) => (
                <div key={item.memory_id} className='flex items-center gap-4 border-b border-divider py-3 last:border-b-0'>
                  <div className='flex min-w-0 flex-1 items-center gap-2'>
                    <KindBadge kind={item.kind} />
                    <div className='min-w-0'>
                      <p className='truncate text-[13.5px] font-medium text-foreground'>{item.title || '(untitled)'}</p>
                      <MemoryLink id={item.memory_id} />
                    </div>
                  </div>
                  <ConfidenceReadout value={item.confidence} />
                  <ScoreReadout value={item.score} max={semanticMax} />
                </div>
              ))}
            </div>
          ) : (
            <p className='mt-3 text-[13px] text-default-500'>No semantic candidates.</p>
          )}
        </div>
      )}

      {result.lexical_enabled && (
        <div className='surface-card p-[22px]'>
          <SectionHeading icon={<ListTree className='h-4 w-4' strokeWidth={1.8} />} title='Lexical candidates' count={lexicalCandidates.length} accent='default' />
          {lexicalCandidates.length > 0 ? (
            <div className='mt-3'>
              {lexicalCandidates.map((item) => (
                <div key={item.memory_id} className='flex items-center gap-4 border-b border-divider py-3 last:border-b-0'>
                  <div className='flex min-w-0 flex-1 items-center gap-2'>
                    <KindBadge kind={item.kind} />
                    <div className='min-w-0'>
                      <p className='truncate text-[13.5px] font-medium text-foreground'>{item.title || '(untitled)'}</p>
                      <MemoryLink id={item.memory_id} />
                    </div>
                  </div>
                  <ConfidenceReadout value={item.confidence} />
                  <ScoreReadout value={item.score} max={lexicalMax} />
                </div>
              ))}
            </div>
          ) : (
            <p className='mt-3 text-[13px] text-default-500'>No lexical candidates.</p>
          )}
        </div>
      )}

      <div className='surface-card p-[22px]'>
        <SectionHeading icon={<Ban className='h-4 w-4' strokeWidth={1.8} />} title='Excluded' count={result.excluded.length} accent='danger' />
        {excludedByReason.length > 0 ? (
          <div className='mt-4 space-y-4'>
            {excludedByReason.map(([reason, entries]) => (
              <div key={reason} className='space-y-2'>
                <div className='flex items-center gap-2'>
                  <span className='rounded-[7px] bg-danger/10 px-2 py-0.5 text-[11.5px] font-semibold text-danger'>
                    {humanizeReason(reason)}
                  </span>
                  <span className='tnum font-mono text-[11px] text-default-400'>{entries.length}</span>
                </div>
                <ul className='space-y-1.5'>
                  {entries.map((item) => (
                    <li key={item.memory_id} className='flex items-start gap-3 rounded-[10px] border border-danger/15 bg-danger/[0.04] px-3.5 py-2.5'>
                      <Ban className='mt-0.5 h-3.5 w-3.5 shrink-0 text-danger' strokeWidth={1.8} />
                      <div className='min-w-0 flex-1'>
                        <p className='truncate text-[13px] font-medium text-foreground'>{item.title || '(untitled)'}</p>
                        <MemoryLink id={item.memory_id} />
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <p className='mt-3 text-[13px] text-default-500'>Nothing was excluded.</p>
        )}
      </div>

      <div className='surface-card overflow-hidden'>
        <button
          type='button'
          onClick={() => setScopeOpen((open) => !open)}
          className='flex w-full items-center justify-between px-5 py-3.5 text-left transition-colors hover:bg-content2/60'
        >
          <span className='flex items-center gap-2.5'>
            <Filter className='h-4 w-4 text-default-400' strokeWidth={1.8} />
            <span className='text-[13.5px] font-semibold text-foreground'>Scope filters</span>
          </span>
          <span className='text-[12px] text-default-400'>{scopeOpen ? 'Hide' : 'Show'}</span>
        </button>
        {scopeOpen && (
          <pre className='overflow-x-auto border-t border-divider px-5 py-4 font-mono text-[11.5px] leading-relaxed text-default-500'>
            {JSON.stringify(result.scope_filters, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}

function ResultsSkeleton() {
  return (
    <div className='space-y-5'>
      <div className='grid gap-3 sm:grid-cols-2 lg:grid-cols-5'>
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className='surface-card h-[88px] animate-pulse bg-content1' />
        ))}
      </div>
      <div className='h-[180px] animate-pulse rounded-[18px] border border-primary/20 bg-primary/[0.04]' />
      <div className='surface-card h-[140px] animate-pulse bg-content1' />
    </div>
  );
}

type ReplayFilters = {
  q: string;
  files: string;
  symbols: string;
};

const DEFAULT_REPLAY: ReplayFilters = { q: '', files: '', symbols: '' };

export default function SearchDebugPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({ queryKey: ['auth', 'me'], queryFn: fetchMe });
  const capabilities = React.useMemo(() => meQuery.data?.capabilities ?? [], [meQuery.data?.capabilities]);

  const [shared, setShared] = useUrlFilters<ReplayFilters>(DEFAULT_REPLAY);
  const [query, setQuery] = React.useState(shared.q);
  const [filePaths, setFilePaths] = React.useState(shared.files);
  const [symbols, setSymbols] = React.useState(shared.symbols);
  const autoRan = React.useRef(false);

  const replay = useMutation<SearchDebugResult, unknown, SearchDebugRequest>({ mutationFn: replaySearchDebug });

  const runReplay = React.useCallback(
    (projectId: string, currentQuery: string, files: string[], syms: string[]) => {
      replay.mutate({ project_id: projectId, team_id: activeTeamId, query: currentQuery, file_paths: files, symbols: syms });
    },
    [replay, activeTeamId],
  );

  const mounted = React.useRef(false);

  React.useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;

      return;
    }

    // reset stale results when the active scope changes
    replay.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProjectId, activeTeamId]);

  React.useEffect(() => {
    if (autoRan.current || !activeProjectId || !shared.q) {
      return;
    }

    autoRan.current = true;
    runReplay(activeProjectId, shared.q, splitTokens(shared.files), splitTokens(shared.symbols));
  }, [activeProjectId, shared.q, shared.files, shared.symbols, runReplay]);

  function handleRun() {
    if (!activeProjectId) {
      return;
    }

    autoRan.current = true;
    setShared({ q: query, files: filePaths, symbols });
    runReplay(activeProjectId, query, splitTokens(filePaths), splitTokens(symbols));
  }

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <PageHeader title='Search Debugger' subtitle='Replay how a query resolves to injected context.' />

        {!activeProjectId ? (
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to replay retrieval for it.'
            icon={<Target className='h-6 w-6' />}
          />
        ) : (
          <>
            <div className='surface-card space-y-4 p-[22px]'>
              <Textarea
                label='Query'
                labelPlacement='outside'
                placeholder='How does the ingest pipeline authorize retrieval?'
                value={query}
                onValueChange={setQuery}
                minRows={3}
                isDisabled={replay.isPending}
              />
              <div className='grid gap-4 sm:grid-cols-2'>
                <Input
                  label='File paths'
                  labelPlacement='outside'
                  placeholder='apps/api/ingest.py, apps/api/retrieval.py'
                  description='Comma or newline separated.'
                  value={filePaths}
                  onValueChange={setFilePaths}
                  isDisabled={replay.isPending}
                />
                <Input
                  label='Symbols'
                  labelPlacement='outside'
                  placeholder='IngestPipeline, authorize_retrieval'
                  description='Comma or newline separated.'
                  value={symbols}
                  onValueChange={setSymbols}
                  isDisabled={replay.isPending}
                />
              </div>
              <div className='flex justify-end'>
                <PrimaryButton startContent={<Play className='h-4 w-4' />} onPress={handleRun} isLoading={replay.isPending}>
                  Run replay
                </PrimaryButton>
              </div>
            </div>

            {replay.isError && (
              <ErrorState title='Replay failed' message={errorMessage(replay.error)} onRetry={handleRun} />
            )}

            {replay.isPending && <ResultsSkeleton />}

            {!replay.isPending && replay.data && <SearchDebugResults result={replay.data} />}
          </>
        )}
      </section>
    </CapabilityGate>
  );
}
