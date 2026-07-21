'use client';

import {
  addToast,
  Button,
  Chip,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Select,
  SelectItem,
  Textarea,
} from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  GitCompareArrows,
  Scale,
} from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useMemoryConflict, useMemoryReview, useResolveMemoryConflict } from '@/hooks/use-memory-review';
import { useProjects } from '@/hooks/use-projects';
import { useTeams } from '@/hooks/use-teams';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type {
  ConflictEvidenceEntry,
  ConflictExistingClaim,
  ConflictResolutionAction,
  MemoryConflictDetail,
  MemoryReviewItem,
  MemoryReviewListParams,
  MemoryReviewOrdering,
} from '@/lib/admin-api';
import {
  actionAllowsMergedText,
  actionRequiresTarget,
  buildConflictResolvePayload,
  CONFLICT_RESOLUTION_ACTIONS,
  cursorFromNextUrl,
  isConflictGoneStatus,
  isConflictResolveFormValid,
  isPreconditionRequiredStatus,
  isStaleConflictStatus,
  MERGED_BODY_MAX_LENGTH,
  MERGED_TITLE_MAX_LENGTH,
  REASON_MAX_LENGTH,
  type ConflictResolveForm,
} from '@/lib/memory-conflict-actions';
import { useOrgStore } from '@/lib/org-store';
import { useUrlFilters } from '@/hooks/use-url-filters';

const ORDERING_OPTIONS: { value: MemoryReviewOrdering; label: string }[] = [
  { value: '-opened_at', label: 'Newest first' },
  { value: 'opened_at', label: 'Oldest first' },
];

const ACTION_META: Record<
  ConflictResolutionAction,
  { label: string; description: string; color: 'primary' | 'success' | 'warning' | 'danger' }
> = {
  publish_candidate: {
    label: 'Publish candidate',
    description: 'Publish the candidate as a new memory and leave every compared memory unchanged.',
    color: 'success',
  },
  merge_candidate: {
    label: 'Merge into memory',
    description: 'Merge the candidate into the selected compared memory as its next version.',
    color: 'primary',
  },
  supersede_memory: {
    label: 'Supersede memory',
    description: 'Publish the candidate and retire the selected compared memory.',
    color: 'warning',
  },
  reject_candidate: {
    label: 'Reject candidate',
    description: 'Reject the candidate and leave every compared memory unchanged.',
    color: 'danger',
  },
};

function extractErrorDetail(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string; code?: string } | undefined;

    if (data?.detail) {
      return data.detail;
    }

    if (data?.code) {
      return data.code;
    }
  }

  if (error instanceof Error) {
    return error.message;
  }

  return 'Unexpected error.';
}

function errorStatus(error: unknown): number | undefined {
  return axios.isAxiosError(error) ? error.response?.status : undefined;
}

type FilterState = {
  project_id: string;
  team_id: string;
  ordering: MemoryReviewOrdering;
};

const DEFAULT_FILTERS: FilterState = {
  project_id: '',
  team_id: '',
  ordering: '-opened_at',
};

interface NamedOption {
  id: string;
  name: string;
}

function FilterBar({
  filters,
  onChange,
  onReset,
  projects,
  teams,
}: {
  filters: FilterState;
  onChange: (next: Partial<FilterState>) => void;
  onReset: () => void;
  projects: NamedOption[];
  teams: NamedOption[];
}) {
  return (
    <div className='surface-card p-4'>
      <div className='grid grid-cols-1 gap-4 md:grid-cols-3'>
        <Select
          label='Project'
          labelPlacement='outside'
          placeholder='Any'
          selectedKeys={filters.project_id ? new Set([filters.project_id]) : new Set<string>()}
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];
            onChange({ project_id: typeof next === 'string' ? next : '' });
          }}
        >
          {projects.map((project) => (
            <SelectItem key={project.id}>{project.name}</SelectItem>
          ))}
        </Select>
        <Select
          label='Team'
          labelPlacement='outside'
          placeholder='Any'
          selectedKeys={filters.team_id ? new Set([filters.team_id]) : new Set<string>()}
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];
            onChange({ team_id: typeof next === 'string' ? next : '' });
          }}
        >
          {teams.map((team) => (
            <SelectItem key={team.id}>{team.name}</SelectItem>
          ))}
        </Select>
        <Select
          label='Sort'
          labelPlacement='outside'
          selectedKeys={new Set([filters.ordering])}
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];

            if (typeof next === 'string') {
              onChange({ ordering: next as MemoryReviewOrdering });
            }
          }}
        >
          {ORDERING_OPTIONS.map((option) => (
            <SelectItem key={option.value}>{option.label}</SelectItem>
          ))}
        </Select>
      </div>
      <div className='mt-3 flex justify-end'>
        <Button size='sm' variant='light' onPress={onReset}>
          Reset filters
        </Button>
      </div>
    </div>
  );
}

function ConflictTable({
  items,
  onReview,
}: {
  items: MemoryReviewItem[];
  onReview: (item: MemoryReviewItem) => void;
}) {
  return (
    <ResponsiveTable minWidth={820}>
      <thead>
        <tr className='border-b border-divider'>
          <th className='px-3 py-2 font-medium text-default-500'>Candidate claim</th>
          <th className='px-3 py-2 font-medium text-default-500'>Compared</th>
          <th className='px-3 py-2 font-medium text-default-500'>Scope</th>
          <th className='px-3 py-2 font-medium text-default-500'>Opened</th>
          <th className='px-3 py-2 text-right font-medium text-default-500'>Actions</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => {
          const firstCompared = item.existing_claims[0];

          return (
            <tr key={item.id} className='border-b border-divider/50'>
              <td className='px-3 py-2'>
                <p className='max-w-[280px] truncate text-foreground' title={item.candidate_claim.title || 'Untitled'}>
                  {item.candidate_claim.title || 'Untitled'}
                </p>
                <Chip size='sm' variant='flat' className='mt-1 capitalize'>
                  {item.candidate_claim.kind}
                </Chip>
              </td>
              <td className='px-3 py-2'>
                <div className='space-y-1'>
                  <Chip size='sm' variant='flat' color='warning'>
                    {item.existing_claims.length} compared
                  </Chip>
                  {firstCompared ? (
                    <p className='max-w-[260px] truncate text-xs text-default-500' title={firstCompared.title}>
                      {firstCompared.title || 'Untitled'}
                    </p>
                  ) : null}
                </div>
              </td>
              <td className='px-3 py-2'>
                <Chip size='sm' variant='flat' className='capitalize'>
                  {item.visibility_scope}
                </Chip>
              </td>
              <td className='whitespace-nowrap px-3 py-2 text-default-700'>
                <TimeStamp value={item.opened_at} />
              </td>
              <td className='px-3 py-2 text-right'>
                <Button
                  size='sm'
                  variant='flat'
                  startContent={<GitCompareArrows className='h-3.5 w-3.5' />}
                  onPress={() => onReview(item)}
                >
                  Review
                </Button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </ResponsiveTable>
  );
}

function EvidenceList({ entries }: { entries?: ConflictEvidenceEntry[] }) {
  if (!entries || entries.length === 0) {
    return <p className='text-xs text-default-500'>No decision-time evidence recorded.</p>;
  }

  return (
    <ul className='space-y-1'>
      {entries.map((entry, index) => (
        <li
          key={entry.reference_id ?? entry.observation_id ?? index}
          className='rounded-medium bg-content2/60 px-2 py-1 text-xs text-default-700'
        >
          <span>{entry.summary}</span>
          {entry.source_kind ? (
            <span className='ml-1 text-default-500'>· {entry.source_kind}</span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ClaimPanel({
  heading,
  title,
  kind,
  body,
  bodyHash,
  evidence,
  accent,
}: {
  heading: string;
  title: string;
  kind: string;
  body?: string;
  bodyHash: string;
  evidence?: ConflictEvidenceEntry[];
  accent: 'primary' | 'default';
}) {
  return (
    <div className='space-y-3 rounded-medium border border-divider p-4'>
      <div className='flex items-center justify-between gap-2'>
        <p className='text-sm font-semibold text-foreground'>{heading}</p>
        <Chip size='sm' variant='flat' color={accent === 'primary' ? 'primary' : 'default'} className='capitalize'>
          {kind}
        </Chip>
      </div>
      <p className='font-medium text-foreground'>{title || 'Untitled'}</p>
      <pre className='max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-medium bg-content2/60 p-3 font-mono text-xs'>
        {body ?? '(body unavailable)'}
      </pre>
      <p className='truncate font-mono text-[11px] text-default-400' title={bodyHash}>
        sha256 {bodyHash.slice(0, 16)}…
      </p>
      <div>
        <p className='mb-1 text-xs font-medium text-default-500'>Evidence</p>
        <EvidenceList entries={evidence} />
      </div>
    </div>
  );
}

function ProvenancePanel({ detail }: { detail: MemoryConflictDetail }) {
  const decision = detail.decision;

  return (
    <div className='space-y-2 rounded-medium border border-divider p-4 text-sm'>
      <p className='text-sm font-semibold text-foreground'>Decision provenance</p>
      <p className='text-xs text-default-500'>
        Applicability verdict:{' '}
        <span className='font-medium text-default-700'>
          {detail.effective_applicability.verdict || 'not evaluated'}
        </span>
      </p>
      {decision ? (
        <dl className='grid grid-cols-1 gap-1 text-xs sm:grid-cols-2'>
          <div>
            <dt className='text-default-500'>Judge status</dt>
            <dd className='text-default-700'>{decision.judge.status}</dd>
          </div>
          <div>
            <dt className='text-default-500'>Evidence tier</dt>
            <dd className='text-default-700'>{decision.evidence_tier || '—'}</dd>
          </div>
          <div>
            <dt className='text-default-500'>Provider</dt>
            <dd className='text-default-700'>{decision.judge.provider ?? '—'}</dd>
          </div>
          <div>
            <dt className='text-default-500'>Model</dt>
            <dd className='text-default-700'>{decision.judge.model ?? '—'}</dd>
          </div>
          {decision.judge.reason ? (
            <div className='sm:col-span-2'>
              <dt className='text-default-500'>Judge reason</dt>
              <dd className='text-default-700'>{decision.judge.reason}</dd>
            </div>
          ) : null}
        </dl>
      ) : (
        <p className='text-xs text-default-500'>No automated decision was recorded for this conflict.</p>
      )}
    </div>
  );
}

function ConflictDetailModal({
  orgId,
  candidateId,
  isOpen,
  onClose,
  onResolved,
}: {
  orgId: string | null;
  candidateId: string | null;
  isOpen: boolean;
  onClose: () => void;
  onResolved: () => void;
}) {
  const detailQuery = useMemoryConflict(orgId, isOpen ? candidateId : null, {
    enabled: isOpen && Boolean(candidateId),
  });
  const resolveMutation = useResolveMemoryConflict(orgId);
  const detail = detailQuery.data;

  const [action, setAction] = React.useState<ConflictResolutionAction>('publish_candidate');
  const [reason, setReason] = React.useState('');
  const [comparedIndex, setComparedIndex] = React.useState(0);
  const [mergedTitle, setMergedTitle] = React.useState('');
  const [mergedBody, setMergedBody] = React.useState('');
  const [resolveError, setResolveError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!isOpen) {
      setAction('publish_candidate');
      setReason('');
      setComparedIndex(0);
      setMergedTitle('');
      setMergedBody('');
      setResolveError(null);
    }
  }, [isOpen]);

  const allowedActions = detail?.resolution_actions ?? CONFLICT_RESOLUTION_ACTIONS;
  const comparedClaims: ConflictExistingClaim[] = detail?.existing_claims ?? [];
  const selectedClaim = comparedClaims[comparedIndex];
  const requiresTarget = actionRequiresTarget(action);
  const allowsMergedText = actionAllowsMergedText(action);

  const form: ConflictResolveForm = {
    action,
    reason,
    targetMemoryId: requiresTarget ? (selectedClaim?.memory_id ?? '') : '',
    mergedTitle,
    mergedBody,
  };

  const canSubmit =
    Boolean(detail) &&
    !resolveMutation.isPending &&
    isConflictResolveFormValid(form);

  async function handleResolve() {
    if (!detail || !candidateId) {
      return;
    }

    setResolveError(null);

    try {
      const result = await resolveMutation.mutateAsync({
        id: candidateId,
        payload: buildConflictResolvePayload(form),
        ifMatch: detail.etag,
      });

      addToast({
        title: `${ACTION_META[action].label} applied`,
        description: result.memory_id ? `Memory ${result.memory_id}` : `Conflict ${result.candidate_id} resolved.`,
        color: 'success',
      });

      onResolved();
      onClose();
    } catch (error) {
      const status = errorStatus(error);

      if (isStaleConflictStatus(status) || isPreconditionRequiredStatus(status)) {
        await detailQuery.refetch();
        setResolveError(
          isPreconditionRequiredStatus(status)
            ? 'The precondition was missing. The conflict set was reloaded — review and submit again.'
            : 'The conflict set changed. It was reloaded — review and submit again.',
        );

        return;
      }

      if (isConflictGoneStatus(status)) {
        addToast({
          title: 'Conflict already resolved',
          description: 'This conflict no longer exists.',
          color: 'warning',
        });
        onResolved();
        onClose();

        return;
      }

      if (status === 401 || status === 403) {
        addToast({ title: 'Not authorized', description: extractErrorDetail(error), color: 'danger' });
        onClose();

        return;
      }

      setResolveError(extractErrorDetail(error));
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      size='5xl'
      scrollBehavior='inside'
      isDismissable={!resolveMutation.isPending}
      hideCloseButton={resolveMutation.isPending}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>Resolve memory conflict</ModalHeader>
            <ModalBody>
              {detailQuery.isLoading ? (
                <p className='py-8 text-center text-sm text-default-500'>Loading conflict…</p>
              ) : detailQuery.isError || !detail ? (
                <ErrorState
                  title='Failed to load conflict'
                  message={
                    detailQuery.error instanceof Error
                      ? detailQuery.error.message
                      : 'The conflict could not be loaded. It may have been resolved.'
                  }
                  onRetry={() => detailQuery.refetch()}
                />
              ) : (
                <div className='space-y-4'>
                  <div className='flex items-start gap-2 rounded-medium border border-warning-200 bg-warning-50 p-3 dark:border-warning-500/30 dark:bg-warning-500/10'>
                    <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-warning-600' />
                    <p className='text-sm text-warning-700 dark:text-warning-400'>
                      Neither claim is settled truth. Resolve on the strength of evidence and provenance, not on which
                      claim arrived first.
                    </p>
                  </div>

                  <div className='grid grid-cols-1 gap-4 lg:grid-cols-2'>
                    <ClaimPanel
                      heading='Candidate claim'
                      title={detail.candidate_claim.title}
                      kind={detail.candidate_claim.kind}
                      body={detail.candidate_claim.body}
                      bodyHash={detail.candidate_claim.body_hash}
                      evidence={detail.candidate_claim.evidence}
                      accent='primary'
                    />
                    <div className='space-y-3'>
                      {comparedClaims.length > 1 ? (
                        <Select
                          label='Compared claim'
                          labelPlacement='outside'
                          selectedKeys={new Set([String(comparedIndex)])}
                          onSelectionChange={(keys) => {
                            const next = Array.from(keys)[0];

                            if (typeof next === 'string') {
                              setComparedIndex(Number.parseInt(next, 10) || 0);
                            }
                          }}
                        >
                          {comparedClaims.map((claim, index) => (
                            <SelectItem key={String(index)}>{claim.title || `Memory ${claim.memory_id}`}</SelectItem>
                          ))}
                        </Select>
                      ) : null}
                      {selectedClaim ? (
                        <ClaimPanel
                          heading='Compared memory claim'
                          title={selectedClaim.title}
                          kind={selectedClaim.kind}
                          body={selectedClaim.body}
                          bodyHash={selectedClaim.body_hash}
                          evidence={selectedClaim.evidence}
                          accent='default'
                        />
                      ) : (
                        <p className='text-sm text-default-500'>No compared claims.</p>
                      )}
                    </div>
                  </div>

                  <ProvenancePanel detail={detail} />

                  <div className='space-y-3 rounded-medium border border-divider p-4'>
                    <p className='text-sm font-semibold text-foreground'>Resolution</p>
                    <Select
                      label='Action'
                      labelPlacement='outside'
                      selectedKeys={new Set([action])}
                      onSelectionChange={(keys) => {
                        const next = Array.from(keys)[0];

                        if (typeof next === 'string') {
                          setAction(next as ConflictResolutionAction);
                          setResolveError(null);
                        }
                      }}
                    >
                      {allowedActions.map((name) => (
                        <SelectItem key={name}>{ACTION_META[name].label}</SelectItem>
                      ))}
                    </Select>
                    <p className='text-xs text-default-500'>{ACTION_META[action].description}</p>
                    {requiresTarget ? (
                      <p className='text-xs text-default-500'>
                        Target memory:{' '}
                        <span className='font-mono text-default-700'>{selectedClaim?.memory_id ?? '—'}</span>
                        {comparedClaims.length > 1 ? ' (change via the compared-claim selector above)' : ''}
                      </p>
                    ) : null}
                    {allowsMergedText ? (
                      <>
                        <Textarea
                          label='Merged title (optional)'
                          labelPlacement='outside'
                          placeholder='Leave blank to keep the target title…'
                          value={mergedTitle}
                          onValueChange={setMergedTitle}
                          minRows={1}
                          maxRows={2}
                          maxLength={MERGED_TITLE_MAX_LENGTH}
                          isDisabled={resolveMutation.isPending}
                        />
                        <Textarea
                          label='Merged body (optional)'
                          labelPlacement='outside'
                          placeholder='Leave blank to keep the candidate body…'
                          value={mergedBody}
                          onValueChange={setMergedBody}
                          minRows={3}
                          maxRows={10}
                          maxLength={MERGED_BODY_MAX_LENGTH}
                          isDisabled={resolveMutation.isPending}
                        />
                      </>
                    ) : null}
                    <Textarea
                      label='Reason'
                      labelPlacement='outside'
                      placeholder='Explain the resolution…'
                      value={reason}
                      onValueChange={setReason}
                      minRows={2}
                      maxRows={6}
                      maxLength={REASON_MAX_LENGTH}
                      isDisabled={resolveMutation.isPending}
                      isRequired
                    />
                    {resolveError ? (
                      <div className='rounded-medium border border-danger-200 bg-danger-50 p-3 dark:border-danger-500/30 dark:bg-danger-500/10'>
                        <p className='text-sm text-danger-600'>{resolveError}</p>
                      </div>
                    ) : null}
                    <Link href='/workflow-runs' className='inline-flex items-center gap-1 text-xs text-primary'>
                      <ExternalLink className='h-3 w-3' />
                      View workflow health for provider or retry lag
                    </Link>
                  </div>
                </div>
              )}
            </ModalBody>
            <ModalFooter>
              <Button color='default' variant='light' onPress={onClose} isDisabled={resolveMutation.isPending}>
                Cancel
              </Button>
              <Button
                color={ACTION_META[action].color}
                startContent={<Scale className='h-4 w-4' />}
                onPress={handleResolve}
                isDisabled={!canSubmit}
                isLoading={resolveMutation.isPending}
              >
                {ACTION_META[action].label}
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function MemoryConflictsPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({ queryKey: ['auth', 'me'], queryFn: fetchMe });

  const capabilities = React.useMemo(() => meQuery.data?.capabilities ?? [], [meQuery.data?.capabilities]);

  const [filters, setFilters, resetFilters] = useUrlFilters<FilterState>(DEFAULT_FILTERS);

  const [cursor, setCursor] = React.useState<string | null>(null);
  const [cursorStack, setCursorStack] = React.useState<string[]>([]);

  const projectsQuery = useProjects(activeOrgId, { pageSize: 100 });
  const teamsQuery = useTeams(activeOrgId, { pageSize: 100 });

  const projects = React.useMemo<NamedOption[]>(
    () => (projectsQuery.data?.results ?? []).map((project) => ({ id: project.id, name: project.name })),
    [projectsQuery.data],
  );
  const teams = React.useMemo<NamedOption[]>(
    () => (teamsQuery.data?.results ?? []).map((team) => ({ id: team.id, name: team.name })),
    [teamsQuery.data],
  );

  const [selectedCandidateId, setSelectedCandidateId] = React.useState<string | null>(null);
  const [detailOpen, setDetailOpen] = React.useState(false);

  const listParams = React.useMemo<MemoryReviewListParams>(
    () => ({
      ordering: filters.ordering,
      ...(filters.project_id ? { project_id: filters.project_id } : {}),
      ...(filters.team_id ? { team_id: filters.team_id } : {}),
      ...(cursor ? { cursor } : {}),
    }),
    [filters, cursor],
  );

  const reviewQuery = useMemoryReview(activeOrgId, listParams, { placeholderData: keepPreviousData });

  const canReview = hasCapability(capabilities, 'memories:review');
  const canAdmin = hasCapability(capabilities, 'memories:admin');

  const isLoading = meQuery.isLoading || (reviewQuery.isLoading && !reviewQuery.data);
  const items = reviewQuery.data?.results ?? [];
  const totalCount = reviewQuery.data?.count ?? 0;
  const nextCursor = cursorFromNextUrl(reviewQuery.data?.next ?? null);

  function applyFilter(next: Partial<FilterState>) {
    setCursor(null);
    setCursorStack([]);
    setFilters(next);
  }

  function handleReset() {
    setCursor(null);
    setCursorStack([]);
    resetFilters();
  }

  function goNext() {
    if (!nextCursor) {
      return;
    }

    setCursorStack((stack) => [...stack, cursor ?? '']);
    setCursor(nextCursor);
  }

  function goPrev() {
    if (cursorStack.length === 0) {
      return;
    }

    const previous = cursorStack[cursorStack.length - 1];
    setCursorStack((stack) => stack.slice(0, -1));
    setCursor(previous === '' ? null : previous);
  }

  function openDetail(item: MemoryReviewItem) {
    setSelectedCandidateId(item.id);
    setDetailOpen(true);
  }

  function closeDetail() {
    setDetailOpen(false);
  }

  const pageNumber = cursorStack.length + 1;

  return (
    <CapabilityGate capabilities={capabilities} required='memories:review'>
      <section className='space-y-6'>
        <PageHeader
          title='Memory Conflicts'
          subtitle='Resolve same-scope conflicts between a proposed candidate and the memories it contradicts.'
          actions={
            <Chip variant='flat' color='warning'>
              {totalCount} open
            </Chip>
          }
        />

        <FilterBar
          filters={filters}
          onChange={applyFilter}
          onReset={handleReset}
          projects={projects}
          teams={teams}
        />

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: 5 }).map((_, index) => (
                    <th key={index} className='px-3 py-2 font-medium text-default-500'>
                      <span className='inline-block h-3 w-16 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={5} />
            </table>
          ) : reviewQuery.isError ? (
            <ErrorState
              title='Failed to load conflicts'
              message={
                reviewQuery.error instanceof Error
                  ? reviewQuery.error.message
                  : 'The conflict inbox could not be loaded.'
              }
              onRetry={() => reviewQuery.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              title='No open conflicts'
              description='Every same-scope contradiction has been resolved. New conflicts appear here as candidates contradict existing memories.'
            />
          ) : (
            <ConflictTable items={items} onReview={openDetail} />
          )}
        </div>

        {!reviewQuery.isError && (totalCount > 0 || cursorStack.length > 0) ? (
          <div className='flex flex-col items-center gap-3'>
            <p className='text-xs text-default-500'>
              {totalCount} open conflict{totalCount === 1 ? '' : 's'} · page {pageNumber}
            </p>
            <div className='flex items-center gap-2'>
              <Button
                size='sm'
                variant='flat'
                startContent={<ChevronLeft className='h-4 w-4' />}
                onPress={goPrev}
                isDisabled={cursorStack.length === 0 || reviewQuery.isFetching}
              >
                Previous
              </Button>
              <Button
                size='sm'
                variant='flat'
                endContent={<ChevronRight className='h-4 w-4' />}
                onPress={goNext}
                isDisabled={!nextCursor || reviewQuery.isFetching}
              >
                Next
              </Button>
            </div>
          </div>
        ) : null}

        {canReview ? (
          <ConflictDetailModal
            orgId={activeOrgId}
            candidateId={selectedCandidateId}
            isOpen={detailOpen}
            onClose={closeDetail}
            onResolved={() => reviewQuery.refetch()}
          />
        ) : null}

        {!canAdmin ? (
          <p className='text-xs text-default-500'>
            Resolving a conflict requires the <span className='font-mono'>memories:admin</span> capability.
          </p>
        ) : null}
      </section>
    </CapabilityGate>
  );
}
