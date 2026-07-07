'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { ArrowLeft, Import } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { StatusPill } from '@/components/ui/status-pill';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useImport } from '@/hooks/use-imports';
import { fetchMe, type MeResponse } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';
import { isTerminalImportStatus, type ImportJob } from '@/lib/admin-api';

function humanizeKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, ' ');

  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function StatTile({ label, value }: { label: string; value: number }) {
  return (
    <div className='rounded-medium border border-divider bg-content2/40 px-4 py-3'>
      <p className='text-[11px] uppercase tracking-wide text-default-500'>
        {label}
      </p>
      <p className='mt-1 text-2xl font-semibold tabular-nums text-foreground'>
        {value.toLocaleString()}
      </p>
    </div>
  );
}

function DetailRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className='grid grid-cols-1 gap-1 border-b border-divider/40 py-2 last:border-b-0 md:grid-cols-[200px_1fr] md:gap-4'>
      <dt className='text-xs font-medium uppercase tracking-wide text-default-500'>
        {label}
      </dt>
      <dd className='break-words text-sm text-foreground'>{children}</dd>
    </div>
  );
}

function toNumberRecord(value: unknown): [string, number][] {
  if (!value || typeof value !== 'object') {
    return [];
  }

  return Object.entries(value as Record<string, unknown>)
    .filter(([, count]) => typeof count === 'number')
    .map(([key, count]) => [key, count as number]);
}

function CountGrid({ entries }: { entries: [string, number][] }) {
  const nonZero = entries.filter(([, count]) => count > 0);
  const shown = nonZero.length > 0 ? nonZero : entries;

  if (shown.length === 0) {
    return <p className='text-sm text-default-500'>—</p>;
  }

  return (
    <div className='grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4'>
      {shown.map(([key, count]) => (
        <div
          key={key}
          className='rounded-medium border border-divider/60 bg-content2/30 px-3 py-2'
        >
          <p className='truncate text-[11px] text-default-500' title={key}>
            {humanizeKey(key)}
          </p>
          <p className='text-lg font-semibold tabular-nums text-foreground'>
            {count.toLocaleString()}
          </p>
        </div>
      ))}
    </div>
  );
}

type UnsupportedEntry = {
  primary: string;
  secondary: string;
};

function describeEntry(entry: unknown): UnsupportedEntry {
  if (entry && typeof entry === 'object') {
    const obj = entry as Record<string, unknown>;
    const primary = String(
      obj.source_id ?? obj.code ?? obj.source_type ?? obj.table ?? 'item',
    );
    const secondary = String(obj.reason ?? obj.message ?? '');

    return { primary, secondary };
  }

  return { primary: String(entry), secondary: '' };
}

function EntryList({ entries }: { entries: unknown[] }) {
  if (entries.length === 0) {
    return <p className='text-sm text-default-500'>None.</p>;
  }

  return (
    <ResponsiveTable minWidth={520}>
      <thead>
        <tr className='border-b border-divider'>
          <th className='px-3 py-2 font-medium text-default-500'>Source</th>
          <th className='px-3 py-2 font-medium text-default-500'>Reason</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry, index) => {
          const { primary, secondary } = describeEntry(entry);

          return (
            <tr key={index} className='border-b border-divider/50'>
              <td className='px-3 py-2 font-mono text-xs text-default-700'>
                {primary}
              </td>
              <td className='px-3 py-2 text-default-600'>
                {secondary ? humanizeKey(secondary) : '—'}
              </td>
            </tr>
          );
        })}
      </tbody>
    </ResponsiveTable>
  );
}

function SourceCounts({
  counts,
}: {
  counts: Record<string, { client_rows?: number }> | undefined;
}) {
  const entries = counts
    ? Object.entries(counts).filter(
        ([, value]) => value && typeof value.client_rows === 'number',
      )
    : [];

  if (entries.length === 0) {
    return <p className='text-sm text-default-500'>No source counts recorded.</p>;
  }

  return (
    <div className='grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4'>
      {entries.map(([table, value]) => (
        <div
          key={table}
          className='rounded-medium border border-divider/60 bg-content2/30 px-3 py-2'
        >
          <p className='truncate text-[11px] text-default-500' title={table}>
            {humanizeKey(table)}
          </p>
          <p className='text-lg font-semibold tabular-nums text-foreground'>
            {(value.client_rows ?? 0).toLocaleString()}
          </p>
        </div>
      ))}
    </div>
  );
}

function ReportSection({ job }: { job: ImportJob }) {
  const report = job.report ?? {};
  const created = toNumberRecord(report.created);
  const duplicates = toNumberRecord(report.duplicates);
  const unsupported = Array.isArray(report.unsupported) ? report.unsupported : [];
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  const redacted = Boolean(report.redactions?.redacted);
  const truncated = Boolean(report.truncations?.truncated);
  const hasReport =
    created.length > 0 ||
    duplicates.length > 0 ||
    unsupported.length > 0 ||
    warnings.length > 0 ||
    Boolean(report.counts);

  if (!hasReport) {
    return (
      <div className='surface-card p-5'>
        <h3 className='mb-3 text-base font-semibold text-foreground'>Report</h3>
        <p className='text-sm text-default-500'>
          {isTerminalImportStatus(job.status)
            ? 'No report was recorded for this migration.'
            : 'The report will appear here once the migration completes.'}
        </p>
      </div>
    );
  }

  return (
    <div className='surface-card space-y-6 p-5'>
      <div className='flex flex-wrap items-center gap-2'>
        <h3 className='text-base font-semibold text-foreground'>Report</h3>
        {redacted && <StatusPill status='redacted' tone='warning' label='Redactions applied' />}
        {truncated && <StatusPill status='truncated' tone='warning' label='Truncations applied' />}
      </div>

      <div className='space-y-2'>
        <p className='text-xs font-medium uppercase tracking-wide text-default-500'>
          Created
        </p>
        <CountGrid entries={created} />
      </div>

      <div className='space-y-2'>
        <p className='text-xs font-medium uppercase tracking-wide text-default-500'>
          Duplicates skipped
        </p>
        <CountGrid entries={duplicates} />
      </div>

      <div className='space-y-2'>
        <p className='text-xs font-medium uppercase tracking-wide text-default-500'>
          Source rows
        </p>
        <SourceCounts counts={report.counts} />
      </div>

      <div className='space-y-2'>
        <p className='text-xs font-medium uppercase tracking-wide text-default-500'>
          Unsupported ({unsupported.length})
        </p>
        <EntryList entries={unsupported} />
      </div>

      <div className='space-y-2'>
        <p className='text-xs font-medium uppercase tracking-wide text-default-500'>
          Warnings ({warnings.length})
        </p>
        <EntryList entries={warnings} />
      </div>
    </div>
  );
}

function StatusHeader({ job }: { job: ImportJob }) {
  const isActive = !isTerminalImportStatus(job.status);

  return (
    <div className='surface-card p-5'>
      <div className='flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between'>
        <div className='space-y-1'>
          <div className='flex flex-wrap items-center gap-2'>
            <Import className='h-5 w-5 text-default-500' />
            <h2 className='font-mono text-sm font-semibold text-foreground'>
              {job.source_store_id || job.id}
            </h2>
            <StatusPill
              status={job.status}
              tone={job.status === 'receiving' ? 'info' : undefined}
            />
            {isActive && (
              <span className='inline-flex items-center gap-1.5 text-xs text-default-500'>
                <span className='inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary' />
                Live
              </span>
            )}
          </div>
          <p className='break-all font-mono text-xs text-default-500'>{job.id}</p>
        </div>
        <div className='space-y-0.5 text-sm text-default-500'>
          <p className='flex items-center justify-end gap-1'>
            Started: <TimeStamp value={job.created_at} />
          </p>
          <p className='flex items-center justify-end gap-1'>
            Updated: <TimeStamp value={job.updated_at} />
          </p>
        </div>
      </div>
    </div>
  );
}

export default function ImportDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';
  const activeOrgId = useOrgStore((state) => state.activeOrgId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const importQuery = useImport(activeOrgId, id);
  const job = importQuery.data;

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <Link
          href='/imports'
          className='inline-flex items-center gap-1 text-sm text-primary hover:underline'
        >
          <ArrowLeft className='h-4 w-4' />
          Back to migrations
        </Link>

        {importQuery.isLoading && (
          <p className='text-default-500'>Loading migration...</p>
        )}

        {importQuery.isError && (
          <ErrorState
            message={
              importQuery.error instanceof Error
                ? importQuery.error.message
                : 'Failed to load migration.'
            }
            onRetry={() => importQuery.refetch()}
          />
        )}

        {job && (
          <>
            <StatusHeader job={job} />

            <div className='surface-card p-5'>
              <h3 className='mb-3 text-base font-semibold text-foreground'>
                Progress
              </h3>
              <div className='grid grid-cols-1 gap-3 sm:grid-cols-3'>
                <StatTile label='Batches applied' value={job.batches_applied} />
                <StatTile label='Rows created' value={job.rows_created} />
                <StatTile label='Rows duplicate' value={job.rows_duplicate} />
              </div>
            </div>

            <div className='surface-card p-5'>
              <h3 className='mb-3 text-base font-semibold text-foreground'>
                Details
              </h3>
              <dl className='flex flex-col'>
                <DetailRow label='Status'>
                  <StatusPill
                    status={job.status}
                    tone={job.status === 'receiving' ? 'info' : undefined}
                  />
                </DetailRow>
                <DetailRow label='Source store'>
                  <span className='font-mono text-xs'>{job.source_store_id || '—'}</span>
                </DetailRow>
                <DetailRow label='Project'>
                  {job.project_name || '—'}
                </DetailRow>
                {job.failure_reason && (
                  <DetailRow label='Failure reason'>
                    <span className='text-danger-600'>{job.failure_reason}</span>
                  </DetailRow>
                )}
                <DetailRow label='Created at'>
                  <TimeStamp value={job.created_at} relative={false} />
                </DetailRow>
                <DetailRow label='Updated at'>
                  <TimeStamp value={job.updated_at} relative={false} />
                </DetailRow>
              </dl>
            </div>

            <ReportSection job={job} />
          </>
        )}

        {importQuery.data === undefined &&
          !importQuery.isLoading &&
          !importQuery.isError && (
            <EmptyState
              title='Migration not found'
              description='This migration may have been removed, or you may not have access to it.'
              icon={<Import className='h-6 w-6' />}
            />
          )}
      </section>
    </CapabilityGate>
  );
}
