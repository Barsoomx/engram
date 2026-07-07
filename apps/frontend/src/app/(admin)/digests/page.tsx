'use client';

import { Select, SelectItem } from '@heroui/react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { BookOpen, Calendar, CheckCircle2 } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { TimeStamp } from '@/components/ui/time-stamp';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  getWeeklyDigest,
  reviewDigest,
  type DigestChangelogItem,
  type DigestCounts,
  type WeeklyDigest,
} from '@/lib/console-api';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const WINDOW_OPTIONS = [
  { value: '7', label: 'Last 7 days' },
  { value: '14', label: 'Last 14 days' },
  { value: '30', label: 'Last 30 days' },
];

function formatDate(iso: string | null): string {
  if (!iso) {
    return '—';
  }

  const d = new Date(iso);

  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatDateRange(start: string | null, end: string | null): string {
  if (!start && !end) {
    return 'Current window';
  }

  return `${formatDate(start)} – ${formatDate(end)}`;
}

type BucketKey = 'added' | 'merged' | 'superseded' | 'retired' | 'refuted';

const BUCKET_STYLES: Record<BucketKey, { label: string; bg: string; text: string }> = {
  added: { label: 'Added', bg: 'bg-success/10', text: 'text-success' },
  merged: { label: 'Merged', bg: 'bg-primary/10', text: 'text-primary-300' },
  superseded: { label: 'Superseded', bg: 'bg-warning/10', text: 'text-warning' },
  retired: { label: 'Retired', bg: 'bg-content3', text: 'text-default-600' },
  refuted: { label: 'Refuted', bg: 'bg-danger/10', text: 'text-danger' },
};

const BUCKET_ORDER: BucketKey[] = ['added', 'merged', 'superseded', 'retired', 'refuted'];

function bucketStyle(bucket: string): { label: string; bg: string; text: string } {
  return BUCKET_STYLES[bucket as BucketKey] ?? { label: bucket, bg: 'bg-content3', text: 'text-default-500' };
}

function CountTile({ label, value, bg, text }: { label: string; value: number; bg: string; text: string }) {
  return (
    <div className='surface-card flex flex-col gap-2.5 p-[18px]'>
      <span className={`inline-flex w-fit rounded-[6px] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] ${bg} ${text}`}>
        {label}
      </span>
      <span className='tnum text-[27px] font-semibold leading-none tracking-[-0.02em] text-foreground'>{value}</span>
    </div>
  );
}

function ChangelogRow({ item }: { item: DigestChangelogItem }) {
  const style = bucketStyle(item.bucket);

  return (
    <Link
      href={`/memories/${item.id}`}
      className='flex items-center gap-3 border-b border-divider py-3 transition-colors last:border-b-0 hover:bg-content2/50'
    >
      <span className={`shrink-0 rounded-[7px] px-2 py-0.5 text-[11px] font-semibold ${style.bg} ${style.text}`}>{style.label}</span>
      <span className='min-w-0 flex-1 truncate text-[13.5px] font-medium text-foreground' title={item.title || undefined}>
        {item.title || '(untitled)'}
      </span>
      <span className='shrink-0 text-[11.5px] text-default-400'>
        <TimeStamp value={item.at} />
      </span>
    </Link>
  );
}

function DigestSkeleton() {
  return (
    <div className='space-y-5'>
      <div className='grid gap-3 sm:grid-cols-3 lg:grid-cols-5'>
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className='surface-card h-[88px] animate-pulse bg-content1' />
        ))}
      </div>
      <div className='surface-card h-[280px] animate-pulse bg-content1' />
    </div>
  );
}

function DigestContent({ digest, onReviewed, canReview }: { digest: WeeklyDigest; onReviewed: () => void; canReview: boolean }) {
  const { counts, changelog, window_start, window_end, window_days, ready } = digest;

  const reviewMutation = useMutation({
    mutationFn: () => reviewDigest(digest.digest_memory_id),
    onSuccess: () => onReviewed(),
  });

  const reviewed = ready || reviewMutation.isSuccess;

  const sortedChangelog = React.useMemo(
    () => [...changelog].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime()),
    [changelog],
  );

  return (
    <div className='space-y-5'>
      <div className='flex items-center justify-between gap-3'>
        <div className='flex items-center gap-2 text-[13px] text-default-500'>
          <Calendar className='h-4 w-4' strokeWidth={1.8} />
          <span>
            {formatDateRange(window_start, window_end)}
            {window_days ? ` · ${window_days}-day window` : ''}
          </span>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-[8px] px-2.5 py-1 text-[11.5px] font-semibold ${
            reviewed ? 'bg-success/10 text-success' : 'bg-content3 text-default-500'
          }`}
        >
          <CheckCircle2 className='h-3.5 w-3.5' strokeWidth={2} />
          {reviewed ? 'Reviewed' : 'Unreviewed'}
        </span>
      </div>

      <div className='grid gap-3 sm:grid-cols-3 lg:grid-cols-5'>
        {BUCKET_ORDER.map((key) => {
          const style = BUCKET_STYLES[key];

          return <CountTile key={key} label={style.label} value={(counts as DigestCounts)[key]} bg={style.bg} text={style.text} />;
        })}
      </div>

      <div className='surface-card p-[22px]'>
        <h3 className='text-[14.5px] font-semibold text-foreground'>Changelog</h3>
        <p className='mb-3 mt-0.5 text-[12px] text-default-500'>All memory changes in this window, newest first.</p>
        {sortedChangelog.length > 0 ? (
          <div>
            {sortedChangelog.map((item) => (
              <ChangelogRow key={item.id} item={item} />
            ))}
          </div>
        ) : (
          <p className='py-6 text-center text-[13px] text-default-400'>No changes recorded in this window.</p>
        )}
        {reviewed ? (
          <p className='mt-4 inline-flex items-center gap-1.5 text-[12.5px] font-medium text-success'>
            <CheckCircle2 className='h-4 w-4' strokeWidth={2} />
            Digest reviewed
          </p>
        ) : canReview ? (
          <div className='mt-4 flex items-center gap-3'>
            <PrimaryButton
              onPress={() => reviewMutation.mutate()}
              isLoading={reviewMutation.isPending}
              startContent={<CheckCircle2 className='h-4 w-4' strokeWidth={2} />}
            >
              Mark reviewed
            </PrimaryButton>
            {reviewMutation.isError && <span className='text-[12px] text-danger'>Failed to mark reviewed.</span>}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function DigestsPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({ queryKey: ['auth', 'me'], queryFn: fetchMe });
  const capabilities = React.useMemo(() => meQuery.data?.capabilities ?? [], [meQuery.data?.capabilities]);

  const [windowDays, setWindowDays] = React.useState(7);

  const digestQuery = useQuery<WeeklyDigest>({
    queryKey: ['digests', 'weekly', activeProjectId, activeTeamId, windowDays],
    queryFn: () => getWeeklyDigest({ projectId: activeProjectId!, teamId: activeTeamId }, windowDays),
    enabled: !!activeProjectId,
  });

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Weekly Digest'
          subtitle='Memory changes merged, retired, and refuted across your project.'
          actions={
            <Select
              aria-label='Digest window'
              size='sm'
              className='w-[160px]'
              selectedKeys={new Set([String(windowDays)])}
              onSelectionChange={(keys) => {
                const next = Array.from(keys)[0];

                if (typeof next === 'string') {
                  setWindowDays(Number(next));
                }
              }}
            >
              {WINDOW_OPTIONS.map((option) => (
                <SelectItem key={option.value}>{option.label}</SelectItem>
              ))}
            </Select>
          }
        />

        {!activeProjectId ? (
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to view its weekly digest.'
            icon={<BookOpen className='h-6 w-6' />}
          />
        ) : digestQuery.isLoading ? (
          <DigestSkeleton />
        ) : digestQuery.isError ? (
          <ErrorState
            title='Failed to load digest'
            message={digestQuery.error instanceof Error ? digestQuery.error.message : 'The weekly digest could not be loaded.'}
            onRetry={() => digestQuery.refetch()}
          />
        ) : digestQuery.data ? (
          <DigestContent
            digest={digestQuery.data}
            onReviewed={() => digestQuery.refetch()}
            canReview={hasCapability(capabilities, 'memories:review')}
          />
        ) : null}
      </section>
    </CapabilityGate>
  );
}
