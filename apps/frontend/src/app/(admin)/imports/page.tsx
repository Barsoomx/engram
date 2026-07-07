'use client';

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { DatabaseZap, Import } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { CopyButton } from '@/components/ui/copy-button';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { PaginationFooter } from '@/components/ui/pagination-footer';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { StatusPill } from '@/components/ui/status-pill';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useImports } from '@/hooks/use-imports';
import { fetchMe, type MeResponse } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';
import { isTerminalImportStatus, type ImportJob } from '@/lib/admin-api';

const PAGE_SIZE = 20;

const SERVER_URL_PLACEHOLDER = 'https://your-engram-server';

function useServerUrl(): string {
  const [url, setUrl] = React.useState(
    process.env.NEXT_PUBLIC_ENGRAM_API_URL || SERVER_URL_PLACEHOLDER,
  );

  React.useEffect(() => {
    if (!process.env.NEXT_PUBLIC_ENGRAM_API_URL && typeof window !== 'undefined') {
      setUrl(window.location.origin);
    }
  }, []);

  return url;
}

function CommandBlock({ command }: { command: string }) {
  return (
    <div className='flex items-center gap-2 rounded-medium border border-divider bg-content2 px-3 py-2'>
      <code className='min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-[12px] text-default-600'>
        {command}
      </code>
      <CopyButton value={command} size={14} label='Copy command' />
    </div>
  );
}

function InstructionsPanel() {
  const url = useServerUrl();

  return (
    <div className='surface-card space-y-5 p-5'>
      <div className='flex items-center gap-2'>
        <DatabaseZap className='h-5 w-5 text-default-500' />
        <h2 className='text-base font-semibold text-foreground'>
          Run a claude-mem migration
        </h2>
      </div>
      <p className='text-sm text-default-500'>
        Migrations run from your machine with the Engram CLI, streaming your
        local <span className='font-mono text-xs'>claude-mem.db</span> to this
        server in batches. Nothing is uploaded until you pass{' '}
        <span className='font-mono text-xs'>--apply</span>.
      </p>

      <ol className='space-y-4'>
        <li className='space-y-2'>
          <div className='flex items-start gap-2'>
            <span className='mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-content3 text-[11px] font-semibold text-default-600'>
              1
            </span>
            <div className='space-y-2'>
              <p className='text-sm text-foreground'>
                Install the Engram CLI and connect it to this server. The API key
                must have the{' '}
                <span className='font-mono text-xs'>memories:admin</span>{' '}
                capability — mint one on the{' '}
                <Link href='/api-keys' className='text-primary hover:underline'>
                  API Keys
                </Link>{' '}
                page.
              </p>
              <CommandBlock
                command={`uvx engram-connect install --server ${url} --api-key <memories:admin key>`}
              />
            </div>
          </div>
        </li>

        <li className='space-y-2'>
          <div className='flex items-start gap-2'>
            <span className='mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-content3 text-[11px] font-semibold text-default-600'>
              2
            </span>
            <div className='space-y-2'>
              <p className='text-sm text-foreground'>
                Preview the migration. A dry run inspects your local database and
                prints the tables, row counts, and projects it would import —
                without writing anything.
              </p>
              <CommandBlock command='engram import claude-mem --dry-run' />
            </div>
          </div>
        </li>

        <li className='space-y-2'>
          <div className='flex items-start gap-2'>
            <span className='mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-content3 text-[11px] font-semibold text-default-600'>
              3
            </span>
            <div className='space-y-2'>
              <p className='text-sm text-foreground'>
                Apply the migration. Progress and the final report appear in the
                history below. If your database holds more than one project, pass{' '}
                <span className='font-mono text-xs'>--project-name</span> to pick
                one.
              </p>
              <p className='text-sm text-default-500'>
                By default the import lands in the project your CLI is connected
                to. To route it into a specific Engram project, pass{' '}
                <span className='font-mono text-xs'>--project &lt;id&gt;</span> —
                copy the id from the{' '}
                <Link href='/projects' className='text-primary hover:underline'>
                  Projects
                </Link>{' '}
                page. You can also point at a different server or key for a single
                run with{' '}
                <span className='font-mono text-xs'>ENGRAM_SERVER_URL</span> and{' '}
                <span className='font-mono text-xs'>ENGRAM_API_KEY</span>.
              </p>
              <CommandBlock command='engram import claude-mem --apply' />
              <CommandBlock command='engram import claude-mem --apply --project-name "my-project"' />
              <CommandBlock command='engram import claude-mem --apply --project <project id>' />
            </div>
          </div>
        </li>
      </ol>
    </div>
  );
}

function importStatusTone(job: ImportJob) {
  return job.status === 'receiving' ? ('info' as const) : undefined;
}

function ImportsTable({ items }: { items: ImportJob[] }) {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={820}>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 font-medium text-default-500'>Source store</th>
            <th className='py-2 px-3 font-medium text-default-500'>Status</th>
            <th className='py-2 px-3 font-medium text-default-500'>Project</th>
            <th className='py-2 px-3 font-medium text-default-500'>Progress</th>
            <th className='py-2 px-3 font-medium text-default-500'>Created</th>
          </tr>
        </thead>
        <tbody>
          {items.map((job) => (
            <tr
              key={job.id}
              className='border-b border-divider/50 hover:bg-content2/40'
            >
              <td className='py-2 px-3'>
                <Link
                  href={`/imports/${job.id}`}
                  className='block max-w-[260px] truncate font-mono text-xs font-medium text-foreground hover:underline'
                  title={job.source_store_id}
                >
                  {job.source_store_id || job.id}
                </Link>
              </td>
              <td className='py-2 px-3'>
                <StatusPill status={job.status} tone={importStatusTone(job)} />
              </td>
              <td className='py-2 px-3 text-default-700'>
                <span
                  className='block max-w-[200px] truncate'
                  title={job.project_name}
                >
                  {job.project_name || '—'}
                </span>
              </td>
              <td className='py-2 px-3 whitespace-nowrap text-default-700'>
                <span className='tabular-nums'>{job.rows_created}</span> created ·{' '}
                <span className='tabular-nums'>{job.rows_duplicate}</span> dup ·{' '}
                <span className='tabular-nums'>{job.batches_applied}</span> batches
              </td>
              <td className='py-2 px-3 whitespace-nowrap text-default-700'>
                <TimeStamp value={job.created_at} />
              </td>
            </tr>
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

export default function ImportsPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [page, setPage] = React.useState(1);
  const params = React.useMemo(
    () => ({ page, pageSize: PAGE_SIZE }),
    [page],
  );

  const importsQuery = useImports(activeOrgId, params, {
    placeholderData: keepPreviousData,
  });

  const items = importsQuery.data?.results ?? [];
  const total = importsQuery.data?.count ?? 0;
  const hasActiveJob = items.some((job) => !isTerminalImportStatus(job.status));

  React.useEffect(() => {
    if (!hasActiveJob) {
      return;
    }

    const handle = setInterval(() => void importsQuery.refetch(), 4000);

    return () => clearInterval(handle);
  }, [hasActiveJob, importsQuery]);

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Migration'
          subtitle='Import your history from claude-mem into Engram and track each migration job.'
        />

        <InstructionsPanel />

        <div className='space-y-3'>
          <h2 className='text-base font-semibold text-foreground'>
            Migration history
          </h2>

          {importsQuery.isLoading ? (
            <div className='surface-card p-2'>
              <table className='w-full border-collapse text-left text-sm'>
                <thead>
                  <tr className='border-b border-divider'>
                    {Array.from({ length: 5 }).map((_, index) => (
                      <th
                        key={index}
                        className='py-2 px-3 font-medium text-default-500'
                      >
                        <span className='inline-block h-3 w-16 rounded-medium bg-content2/60' />
                      </th>
                    ))}
                  </tr>
                </thead>
                <TableRowSkeleton columns={5} />
              </table>
            </div>
          ) : importsQuery.isError && !importsQuery.data ? (
            <ErrorState
              message={
                importsQuery.error instanceof Error
                  ? importsQuery.error.message
                  : 'Failed to load migrations.'
              }
              onRetry={() => importsQuery.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              title='No migrations yet'
              description='Run the CLI import above to migrate your claude-mem history. Jobs appear here as they run.'
              icon={<Import className='h-6 w-6' />}
            />
          ) : (
            <div className='space-y-3'>
              <ImportsTable items={items} />
              <PaginationFooter
                page={page}
                pageSize={PAGE_SIZE}
                total={total}
                noun='migration'
                onPageChange={setPage}
                isDisabled={importsQuery.isFetching}
              />
            </div>
          )}
        </div>
      </section>
    </CapabilityGate>
  );
}
